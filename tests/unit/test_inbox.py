"""Tests for the polite agent-to-agent inbox (#296).

Covers the inbox store (schema, atomic write, ordering, dead-letter), the
``prompt_is_empty`` collision detector against real capture-pane signatures,
and the flush drain (gating, batch coalescing, broadcast, attempt cap).
"""

import json
from types import SimpleNamespace

import pytest

from agentwire import inbox, prompt_router


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    """Point the inbox + events at a throwaway dir."""
    root = tmp_path / "inbox"
    monkeypatch.setattr(inbox, "INBOX_ROOT", root)
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    return root


# =============================================================================
# prompt_is_empty / input_box_content — real capture signatures
# =============================================================================

RULE = "─" * 60

EMPTY_BOX = f"""\
some agent output above
{RULE}
❯
{RULE}
  ~/projects/jordan  main          opus  $339
"""

DRAFT_BOX = f"""\
output
{RULE}
❯ this is my half typed message the human is writing
{RULE}
  ~/projects/jordan  main
"""

QUEUED_BOX = f"""\
output
{RULE}
❯ Press up to edit queued messages
{RULE}
  status bar
"""

WRAPPED_DRAFT = f"""\
output
{RULE}
❯ a long draft that wrapped onto
  a second visible line in the box
{RULE}
  status bar
"""

DIALOG = """\
 Do you want to proceed?
 ❯ 1. Yes
   2. No
 Esc to cancel
"""


class TestInputBox:
    def test_empty_box_is_empty(self):
        assert prompt_router.input_box_content(EMPTY_BOX) == ""

    def test_draft_is_non_empty(self):
        assert prompt_router.input_box_content(DRAFT_BOX).startswith("this is my")

    def test_queued_placeholder_treated_as_content(self):
        # Busy-state placeholder is NOT empty → defer, not clobber.
        assert prompt_router.input_box_content(QUEUED_BOX) == "Press up to edit queued messages"

    def test_is_queued_placeholder(self):
        # Loose match: catches singular/plural and a reworded "↑/N" variant.
        assert prompt_router.is_queued_placeholder("Press up to edit queued messages")
        assert prompt_router.is_queued_placeholder("Press ↑ to edit 2 queued messages")
        assert prompt_router.is_queued_placeholder("1 queued message")
        # A real human draft is NOT a placeholder (so it still penalizes/protects).
        assert not prompt_router.is_queued_placeholder("this is my half typed message")
        assert not prompt_router.is_queued_placeholder("")

    def test_wrapped_draft_non_empty(self):
        content = prompt_router.input_box_content(WRAPPED_DRAFT)
        assert "second visible line" in content

    def test_no_box_returns_none(self):
        assert prompt_router.input_box_content(DIALOG) is None
        assert prompt_router.input_box_content("just some text\nno rules here") is None

    def test_ansi_is_stripped(self):
        colored = f"output\n\x1b[38;5;244m{RULE}\x1b[39m\n\x1b[39m❯ \n\x1b[38;5;244m{RULE}\x1b[39m\n status"
        assert prompt_router.input_box_content(colored) == ""

    def test_prompt_is_empty_gate(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_capture", lambda t: EMPTY_BOX)
        assert prompt_router.prompt_is_empty("s", 0) is True
        monkeypatch.setattr(prompt_router, "_capture", lambda t: DRAFT_BOX)
        assert prompt_router.prompt_is_empty("s", 0) is False
        monkeypatch.setattr(prompt_router, "_capture", lambda t: DIALOG)
        assert prompt_router.prompt_is_empty("s", 0) is False


# =============================================================================
# Enqueue / store
# =============================================================================


class TestEnqueue:
    def test_write_and_read(self, isolate):
        written = inbox.enqueue("sess-a", "hello", kind="done", sender="orch")
        assert len(written) == 1
        msg = written[0]
        assert msg.to == "sess-a" and msg.sender == "orch" and msg.kind == "done"
        data = json.loads(msg.path.read_text())
        assert data["from"] == "orch" and data["text"] == "hello" and data["attempts"] == 0

    def test_ordering_by_filename(self, isolate):
        inbox.enqueue("s", "first", sender="x")
        inbox.enqueue("s", "second", sender="x")
        inbox.enqueue("s", "third", sender="x")
        texts = [m.text for m in inbox.list_messages("s")]
        assert texts == ["first", "second", "third"]

    def test_invalid_kind_rejected(self, isolate):
        with pytest.raises(ValueError):
            inbox.enqueue("s", "x", kind="bogus", sender="x")

    def test_empty_text_rejected(self, isolate):
        with pytest.raises(ValueError):
            inbox.enqueue("s", "   ", sender="x")

    def test_render_prefix(self, isolate):
        msg = inbox.enqueue("s", "PR drafted", kind="done", sender="worker")[0]
        assert msg.render() == "[MSG from worker · done] PR drafted"

    def test_worktree_name_nests(self, isolate):
        inbox.enqueue("proj/feature-x", "hi", sender="x")
        assert (isolate / "proj" / "feature-x").is_dir()
        assert [m.text for m in inbox.list_messages("proj/feature-x")] == ["hi"]


# =============================================================================
# Broadcast
# =============================================================================


class TestBroadcast:
    def test_at_all_excludes_sender(self, isolate, monkeypatch):
        monkeypatch.setattr(inbox, "_live_agent_sessions", lambda: ["a", "b", "orch"])
        written = inbox.enqueue("@all", "team update", sender="orch")
        assert sorted(m.to for m in written) == ["a", "b"]

    def test_literal_target(self, isolate, monkeypatch):
        monkeypatch.setattr(inbox, "_live_agent_sessions", lambda: ["a", "b"])
        written = inbox.enqueue("a", "x", sender="orch")
        assert [m.to for m in written] == ["a"]


# =============================================================================
# Flush — gating, batching, dead-letter
# =============================================================================


def _patch_delivery(monkeypatch, empty=True, deliver=(True, "delivered")):
    from agentwire import usage_limit
    monkeypatch.setattr(usage_limit, "_capture", lambda s: "dummy screen")
    monkeypatch.setattr(
        prompt_router, "input_box_content",
        lambda vis: "" if empty else "draft content",
    )
    monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
    monkeypatch.setattr(prompt_router, "prompt_is_empty", lambda s, p=0: empty)
    sent = []
    monkeypatch.setattr(
        prompt_router, "safe_deliver",
        lambda s, p, text: (sent.append(text) or deliver),
    )
    return sent


class TestFlush:
    def test_delivers_when_empty(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        sent = _patch_delivery(monkeypatch, empty=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 1 and not res["deferred"]
        assert len(sent) == 1
        assert inbox.list_messages("s") == []

    def test_defers_when_not_empty(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        sent = _patch_delivery(monkeypatch, empty=False)
        res = inbox.flush_session("s")
        assert res["deferred"] and res["reason"] == "box_not_empty"
        assert sent == []
        # message survives, attempts bumped
        msgs = inbox.list_messages("s")
        assert len(msgs) == 1 and msgs[0].attempts == 1

    def test_defers_when_safe_deliver_refuses(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=True, deliver=(False, "target_parked"))
        res = inbox.flush_session("s")
        assert res["deferred"] and res["reason"] == "target_parked"
        assert inbox.list_messages("s")[0].attempts == 1

    def test_batch_coalesces(self, isolate, monkeypatch):
        inbox.enqueue("s", "one", sender="x")
        inbox.enqueue("s", "two", sender="x")
        inbox.enqueue("s", "three", sender="x")
        sent = _patch_delivery(monkeypatch, empty=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 3
        assert len(sent) == 1  # single paste
        assert sent[0].count("[MSG from") == 3
        assert inbox.list_messages("s") == []

    def test_empty_inbox_noop(self, isolate, monkeypatch):
        _patch_delivery(monkeypatch, empty=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 0 and res["reason"] == "empty"

    def test_attempt_cap_dead_letters(self, isolate, monkeypatch):
        inbox.enqueue("s", "stuck", sender="x")
        _patch_delivery(monkeypatch, empty=False)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert inbox.list_messages("s") == []  # drained from pending
        dead = list(inbox.dead_dir("s").glob("*.json"))
        assert len(dead) == 1
        assert json.loads(dead[0].read_text())["attempts"] == inbox.MAX_ATTEMPTS

    def test_tick_skips_reserved_dirs(self, isolate, monkeypatch):
        inbox.enqueue("s", "stuck", sender="x")
        _patch_delivery(monkeypatch, empty=False)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        # dead/ now has a json; tick must not treat "s/dead" as a session
        sessions = inbox._iter_pending_sessions()
        assert "s/dead" not in sessions
        assert sessions == []


class TestDead:
    def test_dead_letter_records_reason_and_ts(self, isolate, monkeypatch):
        inbox.enqueue("s", "stuck", sender="x")
        _patch_delivery(monkeypatch, empty=False)  # box_not_empty every pass
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        dead = inbox.list_dead("s")
        assert len(dead) == 1
        assert dead[0].reason == "box_not_empty"
        assert dead[0].dead_ts > 0
        assert dead[0].attempts == inbox.MAX_ATTEMPTS

    def test_dead_letter_carries_safe_deliver_reason(self, isolate, monkeypatch):
        inbox.enqueue("s", "stuck", sender="x")
        _patch_delivery(monkeypatch, empty=True, deliver=(False, "target_parked"))
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        dead = inbox.list_dead("s")
        assert len(dead) == 1 and dead[0].reason == "target_parked"

    def test_list_dead_empty(self, isolate):
        assert inbox.list_dead("nobody") == []

    def test_dead_sessions_enumerates(self, isolate, monkeypatch):
        inbox.enqueue("a", "x", sender="z")
        inbox.enqueue("proj/feature-x", "y", sender="z")
        _patch_delivery(monkeypatch, empty=False)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("a")
            inbox.flush_session("proj/feature-x")
        assert inbox.dead_sessions() == ["a", "proj/feature-x"]
        # a session with only pending (no dead) is not listed
        inbox.enqueue("b", "live", sender="z")
        assert "b" not in inbox.dead_sessions()

    def test_dead_sessions_empty_when_no_dead(self, isolate):
        inbox.enqueue("a", "x", sender="z")
        assert inbox.dead_sessions() == []

    # -- purge_dead -----------------------------------------------------------

    def _seed_dead(self, session, dead_ts, tag):
        """Write a corpse straight into a session's dead/ dir with a chosen dead_ts."""
        msg = inbox.Message(
            id=f"{dead_ts}-{tag}", sender="w", to=session, kind="done",
            text=f"corpse {tag}", ts=dead_ts, attempts=inbox.MAX_ATTEMPTS,
            reason="box_not_empty", dead_ts=dead_ts,
        )
        inbox._write_message(inbox.dead_dir(session) / f"{msg.id}.json", msg)

    def test_purge_dead_all(self, isolate):
        self._seed_dead("a", 1000, "x")
        self._seed_dead("b", 2000, "y")
        assert inbox.purge_dead() == 2
        assert inbox.list_dead("a") == [] and inbox.list_dead("b") == []

    def test_purge_dead_scoped(self, isolate):
        self._seed_dead("a", 1000, "x")
        self._seed_dead("b", 2000, "y")
        assert inbox.purge_dead("a") == 1
        assert inbox.list_dead("a") == []
        assert len(inbox.list_dead("b")) == 1  # other session untouched

    def test_purge_dead_before_ms_keeps_recent(self, isolate):
        self._seed_dead("a", 1_000, "old")
        self._seed_dead("a", 9_000, "recent")
        # cutoff 5_000: clears the old (died <5000), keeps the recent (>=5000)
        assert inbox.purge_dead("a", before_ms=5_000) == 1
        survivors = inbox.list_dead("a")
        assert len(survivors) == 1 and survivors[0].dead_ts == 9_000

    def test_purge_dead_before_ms_drops_preschema(self, isolate):
        self._seed_dead("a", 0, "preschema")  # dead_ts 0 = infinitely old
        assert inbox.purge_dead("a", before_ms=5_000) == 1
        assert inbox.list_dead("a") == []

    def test_purge_dead_nested_session(self, isolate):
        self._seed_dead("proj/feature-x", 1000, "z")
        assert inbox.purge_dead() == 1  # global rglob reaches nested dead/
        assert inbox.list_dead("proj/feature-x") == []

    def test_purge_dead_noop(self, isolate):
        assert inbox.purge_dead() == 0
        assert inbox.purge_dead("nobody") == 0


class TestTick:
    def test_tick_drains_all(self, isolate, monkeypatch):
        inbox.enqueue("a", "x", sender="z")
        inbox.enqueue("b", "y", sender="z")
        _patch_delivery(monkeypatch, empty=True)
        res = inbox.tick()
        assert len(res["flushed"]) == 2
        assert inbox.list_messages("a") == [] and inbox.list_messages("b") == []


class TestEscalation:
    def test_busy_screen_defers_target_busy(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: None)
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)

        res = inbox.flush_session("s")
        assert res["deferred"]
        assert res["reason"] == "target_busy"
        # attempts must NOT increment for target_busy
        assert inbox.list_messages("s")[0].attempts == 0

    def test_done_under_threshold_defers_box_not_empty(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: "draft content")
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)

        res = inbox.flush_session("s")
        assert res["deferred"]
        assert res["reason"] == "box_not_empty"
        assert inbox.list_messages("s")[0].attempts == 1

    def test_done_on_occupied_box_always_defers_to_protect_drafts(self, isolate, monkeypatch):
        msgs = inbox.enqueue("s", "PR done", kind="done", sender="worker")
        msg = msgs[0]
        msg.attempts = 10
        inbox._write_message(msg.path, msg)

        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: "draft content")
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)
        sent = []
        monkeypatch.setattr(prompt_router, "safe_deliver", lambda s, p, text: (sent.append(text) or (True, "delivered")))

        res = inbox.flush_session("s")
        assert res["deferred"]
        assert res["reason"] == "box_not_empty"
        assert len(sent) == 0
        assert inbox.list_messages("s")[0].attempts == 11

    def test_done_over_threshold_but_busy_still_defers(self, isolate, monkeypatch):
        msgs = inbox.enqueue("s", "PR done", kind="done", sender="worker")
        msg = msgs[0]
        msg.attempts = 10
        inbox._write_message(msg.path, msg)

        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: None)
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)

        res = inbox.flush_session("s")
        assert res["deferred"]
        assert res["reason"] == "target_busy"
        # attempts must NOT increment for target_busy
        assert inbox.list_messages("s")[0].attempts == 10

    def test_done_over_threshold_on_non_agent_still_defers(self, isolate, monkeypatch):
        msgs = inbox.enqueue("s", "PR done", kind="done", sender="worker")
        msg = msgs[0]
        msg.attempts = 10
        inbox._write_message(msg.path, msg)

        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: "draft content")
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: False)

        res = inbox.flush_session("s")
        assert res["deferred"]
        assert res["reason"] == "box_not_empty"
        assert inbox.list_messages("s")[0].attempts == 11


class TestQueuedPlaceholderDefer:
    """B: the 'Press up to edit queued messages' placeholder is a BUSY signal,
    not a human draft — it defers WITHOUT penalty (like target_busy), so a
    generating-with-queued session never burns report-backs toward dead-letter.
    The collision guard is untouched: we still never paste into a non-empty box."""

    def _placeholder_box(self, monkeypatch):
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(
            prompt_router, "input_box_content",
            lambda vis: "Press up to edit queued messages",
        )
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)

    def test_placeholder_defers_without_penalty(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        self._placeholder_box(monkeypatch)
        res = inbox.flush_session("s")
        assert res["deferred"]
        assert res["reason"] == "queued_placeholder"
        assert inbox.list_messages("s")[0].attempts == 0  # never penalized

    def test_placeholder_never_dead_letters(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        self._placeholder_box(monkeypatch)
        for _ in range(inbox.MAX_ATTEMPTS + 5):
            inbox.flush_session("s")
        pending = inbox.list_messages("s")
        assert len(pending) == 1 and pending[0].attempts == 0
        assert inbox.list_dead("s") == []  # stayed pending, surfaced by doctor

    def test_placeholder_never_pastes(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        self._placeholder_box(monkeypatch)
        sent = []
        monkeypatch.setattr(
            prompt_router, "safe_deliver",
            lambda s, p, text: (sent.append(text) or (True, "delivered")),
        )
        inbox.flush_session("s")
        assert sent == []  # box non-empty → never delivered into


class TestDeadLetterEscalation:
    """A: a load-bearing report-back (done/request/escalation) that dead-letters
    emails the owner out-of-band; note does not. Escalation is best-effort and
    must never break the drain."""

    def _occupied_agent(self, monkeypatch):
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: "human draft")
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)

    def _capture_email(self, monkeypatch, sink):
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **kw: sink.append(kw) or SimpleNamespace(success=True),
        )

    def test_done_dead_letter_emails_owner(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR #312 merged", kind="done", sender="worker")
        self._occupied_agent(monkeypatch)
        sent = []
        self._capture_email(monkeypatch, sent)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert len(sent) == 1
        assert "done" in sent[0]["subject"] and "worker" in sent[0]["subject"]
        assert "PR #312 merged" in sent[0]["body"]
        assert len(inbox.list_dead("s")) == 1  # still archived for audit

    def test_request_and_escalation_also_email(self, isolate, monkeypatch):
        inbox.enqueue("s", "need creds", kind="request", sender="w")
        inbox.enqueue("s", "stuck!", kind="escalation", sender="w")
        self._occupied_agent(monkeypatch)
        sent = []
        self._capture_email(monkeypatch, sent)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        kinds = sorted(k["subject"].split("undelivered ")[1].split(":")[0] for k in sent)
        assert kinds == ["escalation", "request"]

    def test_note_dead_letter_does_not_email(self, isolate, monkeypatch):
        inbox.enqueue("s", "fyi", kind="note", sender="worker")
        self._occupied_agent(monkeypatch)
        sent = []
        self._capture_email(monkeypatch, sent)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert sent == []  # note is fire-and-forget
        assert len(inbox.list_dead("s")) == 1  # still dead-lettered, just silent

    def test_escalation_failure_never_breaks_drain(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR merged", kind="done", sender="worker")
        self._occupied_agent(monkeypatch)

        def boom(**kw):
            raise RuntimeError("resend down")

        monkeypatch.setattr("agentwire.channels.email.send_email", boom)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert len(inbox.list_dead("s")) == 1  # drain survived; corpse archived


class TestIdempotentDelivery:
    """#621: a delivery_unverified false-negative must NOT re-inject a landed
    paste. If the rendered message is on scrollback, treat it as delivered and
    consume it — per-message, so a partial landing consumes only the visible
    subset."""

    def _patch(self, monkeypatch, deliver, scrollback):
        from agentwire import session_ready, usage_limit
        monkeypatch.setattr(usage_limit, "_capture", lambda s: "dummy screen")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: "")
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
        sent = []
        monkeypatch.setattr(
            prompt_router, "safe_deliver",
            lambda s, p, text: (sent.append(text) or deliver),
        )
        monkeypatch.setattr(session_ready, "scrollback", lambda s, p=0: scrollback)
        return sent

    def test_unverified_but_landed_is_consumed(self, isolate, monkeypatch):
        msgs = inbox.enqueue("s", "PR drafted", kind="done", sender="w")
        cap = msgs[0].render()  # the paste landed on scrollback
        self._patch(monkeypatch, deliver=(False, "delivery_unverified"), scrollback=cap)
        res = inbox.flush_session("s")
        assert res["delivered"] == 1 and not res["deferred"]
        assert inbox.list_messages("s") == []  # consumed, not re-injected

    def test_unverified_and_not_landed_stays_pending(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR drafted", kind="done", sender="w")
        self._patch(monkeypatch, deliver=(False, "delivery_unverified"),
                    scrollback="nothing relevant here")
        res = inbox.flush_session("s")
        assert res["deferred"] and res["reason"] == "delivery_unverified"
        msgs = inbox.list_messages("s")
        assert len(msgs) == 1 and msgs[0].attempts == 1  # penalized, retried

    def test_per_message_keying_consumes_only_visible(self, isolate, monkeypatch):
        a = inbox.enqueue("s", "alpha report", kind="done", sender="w")[0]
        inbox.enqueue("s", "beta report", kind="done", sender="w")
        # Only the first message's fragment is on scrollback.
        self._patch(monkeypatch, deliver=(False, "delivery_unverified"),
                    scrollback=a.render())
        res = inbox.flush_session("s")
        assert res["delivered"] == 1 and res["deferred"]
        remaining = inbox.list_messages("s")
        assert len(remaining) == 1 and "beta" in remaining[0].text

    def test_long_sender_prefix_does_not_collide(self, isolate, monkeypatch):
        # Worktree senders fill the old 32-char fragment entirely with the
        # "[MSG from <sender> · <kind>] " header, so two same-sender same-kind
        # messages shared a fragment and the 2nd was silently consumed against
        # the 1st's scrollback line. Full-line keying keeps them distinct: only
        # the first (on scrollback) is consumed; the second stays pending.
        sender = "agentwire-dev-fix-621-inbox"  # 27 chars — blows the 32 budget
        a = inbox.enqueue("orch", "first report alpha", kind="done", sender=sender)[0]
        inbox.enqueue("orch", "second report beta", kind="done", sender=sender)
        self._patch(monkeypatch, deliver=(False, "delivery_unverified"),
                    scrollback=a.render())  # only the FIRST line is visible
        inbox.flush_session("orch")
        remaining = inbox.list_messages("orch")
        assert len(remaining) == 1, "second same-sender message must NOT be dropped"
        assert "second report beta" in remaining[0].text
        assert remaining[0].attempts == 1  # penalized/retried, not silently lost

    def test_placeholder_does_not_falsely_consume(self, isolate, monkeypatch):
        # A bare "[Pasted text ...]" placeholder must NOT mark every message
        # visible (the message_visible fallback is intentionally skipped).
        inbox.enqueue("s", "PR drafted", kind="done", sender="w")
        self._patch(monkeypatch, deliver=(False, "delivery_unverified"),
                    scrollback="[Pasted text #1 +40 lines]")
        res = inbox.flush_session("s")
        assert res["deferred"] and not res.get("delivered")
        assert len(inbox.list_messages("s")) == 1

    def test_predelivery_dedup_consumes_without_pasting(self, isolate, monkeypatch):
        # A prior tick landed the paste; on the next tick the message is already
        # on scrollback, so we consume it WITHOUT pasting again.
        m = inbox.enqueue("s", "PR drafted", kind="done", sender="w")[0]
        sent = self._patch(monkeypatch, deliver=(True, "delivered"),
                           scrollback=m.render())
        res = inbox.flush_session("s")
        assert res["delivered"] == 1 and not res["deferred"]
        assert sent == []  # never re-pasted
        assert inbox.list_messages("s") == []


class TestPurgePending:
    def test_purge_drops_pending(self, isolate):
        inbox.enqueue("s", "one", sender="x")
        inbox.enqueue("s", "two", sender="x")
        assert inbox.purge_pending("s") == 2
        assert inbox.list_messages("s") == []

    def test_purge_leaves_ingest_and_dead(self, isolate, monkeypatch):
        inbox.enqueue("s", "active", sender="x")
        inbox.enqueue("s", "passive", kind="ingest", sender="x")
        # dead-letter one
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: "draft")
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
        msgs = inbox.enqueue("s", "doomed", kind="done", sender="x")
        msgs[0].attempts = inbox.MAX_ATTEMPTS - 1
        inbox._write_message(msgs[0].path, msgs[0])
        inbox.flush_session("s")
        assert len(inbox.list_dead("s")) == 1
        # purge clears only the active queue
        removed = inbox.purge_pending("s")
        assert removed == 1  # only the "active" note
        assert inbox.list_ingest("s")  # ingest untouched
        assert len(inbox.list_dead("s")) == 1  # dead untouched

    def test_purge_noop(self, isolate):
        assert inbox.purge_pending("nobody") == 0


class TestForceFlush:
    def test_force_pastes_despite_nonempty_box(self, isolate, monkeypatch):
        inbox.enqueue("s", "urgent", kind="done", sender="w")
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: "draft")
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
        sent = []
        monkeypatch.setattr(
            prompt_router, "safe_deliver",
            lambda s, p, text: (sent.append(text) or (True, "delivered")),
        )
        res = inbox.flush_session("s", force=True)
        assert res["delivered"] == 1 and len(sent) == 1
        assert inbox.list_messages("s") == []


class TestGcSender:
    def test_gc_dead_letters_load_bearing(self, isolate, monkeypatch):
        emailed = []
        monkeypatch.setattr(inbox, "_escalate_dead_letter",
                            lambda m, r: emailed.append((m.kind, r)))
        inbox.enqueue("orch", "PR drafted", kind="done", sender="worker")
        inbox.enqueue("orch", "need review", kind="request", sender="worker")
        res = inbox.gc_sender("worker")
        assert res["dead"] == 2 and res["dropped"] == 0
        assert inbox.list_messages("orch") == []
        assert len(inbox.list_dead("orch")) == 2
        assert emailed and all(r == "sender_exited" for _, r in emailed)

    def test_gc_drops_non_load_bearing(self, isolate, monkeypatch):
        monkeypatch.setattr(inbox, "_escalate_dead_letter", lambda m, r: None)
        inbox.enqueue("orch", "fyi", kind="note", sender="worker")
        res = inbox.gc_sender("worker")
        assert res["dropped"] == 1 and res["dead"] == 0
        assert inbox.list_messages("orch") == []
        assert inbox.list_dead("orch") == []

    def test_gc_skips_recipient_with_held_lock(self, isolate, monkeypatch):
        # A flush draining this inbox holds the per-session lock; gc must NOT
        # dead-letter (and email) a message that flush is mid-delivery on.
        emailed = []
        monkeypatch.setattr(inbox, "_escalate_dead_letter",
                            lambda m, r: emailed.append(m.id))
        inbox.enqueue("orch", "in flight", kind="done", sender="worker")
        held = inbox._acquire_lock("orch")  # simulate an in-flight flush
        try:
            res = inbox.gc_sender("worker")
        finally:
            inbox._release_lock(held)
        assert res == {"dead": 0, "dropped": 0}
        assert emailed == []  # no false "never delivered" escalation
        assert len(inbox.list_messages("orch")) == 1  # left for flush to deliver

    def test_gc_ignores_other_senders_and_ingest(self, isolate):
        inbox.enqueue("orch", "keep me", kind="done", sender="other")
        inbox.enqueue("orch", "passive", kind="ingest", sender="worker")
        res = inbox.gc_sender("worker")
        assert res == {"dead": 0, "dropped": 0}
        assert len(inbox.list_messages("orch")) == 1  # other sender's done kept
        assert inbox.list_ingest("orch")  # ingest untouched
