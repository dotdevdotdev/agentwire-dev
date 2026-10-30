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
    # Positive-existence gate (#694) defaults to "unknown" (tmux unreachable)
    # so the busy/refusal scenarios below exercise the ordinary gates without
    # the host's real session list interfering. Gone-target behavior is tested
    # explicitly in TestTargetGone with a controlled live set.
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
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


DIALOG = """\
 Do you want to proceed?
 ❯ 1. Yes
   2. No
 Esc to cancel
"""


class TestInputBox:
    def test_prompt_is_empty_gate(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_capture", lambda t, **kw: EMPTY_BOX)
        assert prompt_router.prompt_is_empty("s", 0) is True
        monkeypatch.setattr(prompt_router, "_capture", lambda t, **kw: DRAFT_BOX)
        assert prompt_router.prompt_is_empty("s", 0) is False
        monkeypatch.setattr(prompt_router, "_capture", lambda t, **kw: DIALOG)
        assert prompt_router.prompt_is_empty("s", 0) is False
        monkeypatch.setattr(prompt_router, "_capture", lambda t, **kw: GHOST_BOX)
        assert prompt_router.prompt_is_empty("s", 0) is True
        monkeypatch.setattr(prompt_router, "_capture", lambda t, **kw: MIXED_BOX)
        assert prompt_router.prompt_is_empty("s", 0) is False


# SGR-preserved captures (tmux capture-pane -e). Claude Code renders the box
# border colored, ghost/autosuggest text dim (ESC[2m), and typed text plain.
DIM_RULE = f"\x1b[38;5;244m{RULE}\x1b[39m"


GHOST_BOX = f"""\
some agent output above
{DIM_RULE}
❯ \x1b[2mTry "fix lint errors"\x1b[22m
{DIM_RULE}
  status bar
"""

MIXED_BOX = f"""\
output
{DIM_RULE}
❯ typed prefix\x1b[2m ghost completion tail\x1b[22m
{DIM_RULE}
  status bar
"""


# =============================================================================
# Flush — gating, batching, dead-letter
# =============================================================================


def _patch_delivery(monkeypatch, empty=True, deliver=(True, "delivered"), box=None):
    from agentwire import usage_limit
    monkeypatch.setattr(usage_limit, "_capture", lambda s, **kw: "dummy screen")
    monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "dummy screen")
    # Default non-empty box content VARIES per sweep (an actively-edited draft);
    # identical content across sweeps is the no-penalty box_static path (#669) —
    # pass a fixed ``box=`` to exercise it.
    calls = {"n": 0}

    def _box(vis):
        if empty:
            return ""
        if box is not None:
            return box
        calls["n"] += 1
        return f"draft content {calls['n']}"

    monkeypatch.setattr(prompt_router, "input_box_content_sgr", _box)
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

    def test_a_deferral_keeps_what_the_drain_saw_and_it_expires(self, isolate, monkeypatch):
        """The inbox records only a length (#1105), so "the box was empty, why
        did it defer?" needs the capture itself: an owner-only file with the
        parsed box and the pane, pruned once it is older than the TTL."""
        import os
        import stat
        import time

        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=False, box="Update installed")
        inbox.flush_session("s")
        root = isolate.parent / "inbox-debug" / "s"
        files = list(root.glob("*.txt"))
        assert len(files) == 1
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        text = files[0].read_text()
        assert "reason=box_not_empty" in text and "'Update installed'" in text
        assert "dummy screen" in text

        old = time.time() - inbox.DEFERRAL_CAPTURE_TTL_S - 60
        os.utime(files[0], (old, old))
        inbox.flush_session("s")
        assert files[0] not in list(root.glob("*.txt"))

    def test_a_delivered_message_leaves_no_capture(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=True)
        inbox.flush_session("s")
        assert not (isolate.parent / "inbox-debug").exists()

    def test_four_plus_coalesce_never_exceeds_line_bound(self, isolate, monkeypatch):
        # #930 regression (the chip regime): 4+ rendered lines in ONE paste
        # collapse to the "[Pasted text]" chip, blinding the #689 stuck test —
        # a swallowed Enter then wedges EVERY message in the paste, permanently
        # (never healed, never dead-lettered, never emailed). The drain must
        # never paste more than PASTE_MAX_LINES messages per blob.
        for i in range(5):
            inbox.enqueue("s", f"report {i}", sender="x")
        sent = _patch_delivery(monkeypatch, empty=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 5 and not res["deferred"]
        assert inbox.list_messages("s") == []
        assert len(sent) == 2  # 3 + 2 — never a 5-line blob
        for paste in sent:
            assert len(paste.split("\n")) <= inbox.PASTE_MAX_LINES

    def test_char_bound_splits_below_line_bound(self, isolate, monkeypatch):
        # #930 regression (the windowing regime): character length governs
        # independently of line count — 530 chars on ONE line windows with no
        # chip and the heal misses. Two messages whose coalesced render
        # exceeds PASTE_MAX_CHARS must split even though 2 ≤ PASTE_MAX_LINES.
        inbox.enqueue("s", "a" * 250, sender="x")
        inbox.enqueue("s", "b" * 250, sender="x")
        sent = _patch_delivery(monkeypatch, empty=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 2 and not res["deferred"]
        assert len(sent) == 2
        for paste in sent:
            assert len(paste) <= inbox.PASTE_MAX_CHARS

    def test_attempt_cap_dead_letters(self, isolate, monkeypatch):
        inbox.enqueue("s", "stuck", sender="x")
        _patch_delivery(monkeypatch, empty=False)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert inbox.list_messages("s") == []  # drained from pending
        dead = list(inbox.dead_dir("s").glob("*.json"))
        assert len(dead) == 1
        assert json.loads(dead[0].read_text())["attempts"] == inbox.MAX_ATTEMPTS


class TestParkedDefer:
    """#872: a usage-limit parked recipient defers WITHOUT penalty.

    ``safe_deliver`` refuses a parked target (pasting would corrupt the resume),
    but ``target_parked`` was penalized, so every ~60s watchdog tick burned an
    attempt and a worker's ``done`` dead-lettered at MAX_ATTEMPTS ≈ 40 min. A
    real park lasts until the usage-limit reset — routinely hours — so any park
    worth the name guaranteed the report-back died. Parked is the *exists but
    can't take it* case (like ``target_busy``), not the ``target_gone`` case.

    Driven through the REAL ``safe_deliver`` and the REAL ``usage_limit``
    park-state file rather than a stubbed refusal, so the park→no-penalty chain
    is exercised end to end and un-parking actually releases the message.
    """

    @pytest.fixture
    def park(self, tmp_path, monkeypatch):
        """Park/un-park a session for real, on a throwaway state dir."""
        from agentwire import session_ready, usage_limit

        state_dir = tmp_path / "usage-limit"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(usage_limit, "STATE_DIR", state_dir)

        # Everything safe_deliver checks BESIDES the park: session exists, pane
        # runs an agent, no live menu, empty box, nothing already on scrollback.
        # The park state is the only variable.
        monkeypatch.setattr(prompt_router, "_session_exists", lambda s: True)
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
        monkeypatch.setattr(prompt_router, "screen_shows_live_menu", lambda v: False)
        monkeypatch.setattr(prompt_router, "_capture", lambda t, **kw: "")
        monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: "")
        monkeypatch.setattr(session_ready, "scrollback", lambda s, p=0, **kw: "")

        sent: list[str] = []
        monkeypatch.setattr(
            session_ready, "send_verified",
            lambda s, text, pane_index=0, **kw: (sent.append(text) or True),
        )

        assert not usage_limit.is_parked("s")  # fixture really is isolated
        return SimpleNamespace(
            sent=sent,
            on=lambda session: usage_limit.write_park_state({"session": session}),
            off=lambda session: usage_limit.state_path(session).unlink(),
        )

    def test_parked_never_dead_letters_then_delivers_on_unpark(self, isolate, park):
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        park.on("s")
        for _ in range(inbox.MAX_ATTEMPTS + 5):
            inbox.flush_session("s")

        pending = inbox.list_messages("s")
        assert len(pending) == 1
        assert pending[0].attempts == 0
        assert pending[0].reason == "target_parked"
        assert inbox.list_dead("s") == []  # survived a park longer than the cap

        # The reset nudge un-parks the session — the held report lands.
        park.off("s")
        res = inbox.flush_session("s")
        assert res["delivered"] == 1 and not res["deferred"]
        assert len(park.sent) == 1 and "PR done" in park.sent[0]
        assert inbox.list_messages("s") == []


class TestTargetGone:
    """#694: a recipient that positively doesn't exist defers as `target_gone`
    — a penalized reason with its own fast cap (GONE_MAX_ATTEMPTS ≈ minutes,
    not the 40-minute busy window) — checked BEFORE the box gate. The original
    hole: capturing a gone session parses as "no box" → `target_busy`, a
    no-penalty defer, so a done to a stale parent sat queued ~24h instead of
    burning out and escalating."""

    def test_fast_dead_letter_window(self, isolate, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: set())
        inbox.enqueue("ghost", "hi", sender="w")
        for _ in range(inbox.GONE_MAX_ATTEMPTS - 1):
            inbox.flush_session("ghost")
            assert len(inbox.list_messages("ghost")) == 1  # grace not yet spent
        inbox.flush_session("ghost")
        assert inbox.list_messages("ghost") == []
        dead = inbox.list_dead("ghost")
        assert len(dead) == 1
        assert dead[0].reason == "target_gone"
        assert dead[0].gone_attempts == inbox.GONE_MAX_ATTEMPTS
        # The whole point: minutes, not the 40-minute busy cap.
        assert inbox.GONE_MAX_ATTEMPTS < inbox.MAX_ATTEMPTS

    def test_unknown_tmux_state_never_fast_kills(self, isolate, monkeypatch):
        # live_sessions() None (server down / no tmux) is an outage, not a gone
        # recipient: the ordinary defer path applies (here: unparseable box →
        # target_busy, no penalty) so a reboot can't nuke queued report-backs.
        monkeypatch.setattr(inbox, "live_sessions", lambda: None)
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "")
        monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: None)
        inbox.enqueue("ghost", "hi", sender="w")
        res = inbox.flush_session("ghost")
        assert res["reason"] == "target_busy"
        msg = inbox.list_messages("ghost")[0]
        assert msg.attempts == 0 and msg.gone_attempts == 0


class TestDeadLetterEscalation:
    """A: a load-bearing report-back (done/request/escalation) that dead-letters
    emails the owner out-of-band; note does not. Escalation is best-effort and
    must never break the drain."""

    def _occupied_agent(self, monkeypatch):
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        # Varying draft per sweep — identical content would hit the no-penalty
        # box_static path (#669) and never dead-letter.
        calls = {"n": 0}

        def _box(vis):
            calls["n"] += 1
            return f"human draft {calls['n']}"

        monkeypatch.setattr(prompt_router, "input_box_content_sgr", _box)
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)

    def _capture_email(self, monkeypatch, sink):
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **kw: sink.append(kw) or SimpleNamespace(success=True),
        )

    def test_large_batch_sends_one_digest_not_one_per_message(self, isolate, monkeypatch):
        # Regression (2026-07-19): a recipient stuck permanently undeliverable
        # (e.g. wrongly parented to a service session) can accumulate a large
        # backlog that all crosses MAX_ATTEMPTS in the same drain pass. That
        # must fire ONE digest email, not one per message (147 individual
        # emails in ~2s was the real incident).
        # A note dead-letters silently: it is fire-and-forget, not in the digest.
        for i in range(5):
            inbox.enqueue("s", f"report {i}", kind="done", sender="w")
        inbox.enqueue("s", "fyi", kind="note", sender="w")
        self._occupied_agent(monkeypatch)
        sent = []
        self._capture_email(monkeypatch, sent)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert len(inbox.list_dead("s")) == 6
        assert len(sent) == 1
        assert "5 undelivered messages" in sent[0]["subject"]


class TestIdempotentDelivery:
    """#621: a delivery_unverified false-negative must NOT re-inject a landed
    paste. If the rendered message is on scrollback, treat it as delivered and
    consume it — per-message, so a partial landing consumes only the visible
    subset."""

    def _patch(self, monkeypatch, deliver, scrollback):
        from agentwire import session_ready, usage_limit
        # #689: "on scrollback" now means outside a parseable input box — a bare
        # rendered line with no box no longer counts (it could be sitting unsent
        # in the box). Model a real delivered state: text above an EMPTY box.
        # #1113: and only the agent's own input echo (a prompt-glyph row) counts.
        scrollback = "\n".join(
            f"❯ {ln}" if ln.startswith("[MSG") else ln for ln in scrollback.split("\n"))
        scrollback = f"{scrollback}\n{RULE}\n❯\n{RULE}"
        monkeypatch.setattr(usage_limit, "_capture", lambda s: "dummy screen")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: "")
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
        sent = []
        monkeypatch.setattr(
            prompt_router, "safe_deliver",
            lambda s, p, text: (sent.append(text) or deliver),
        )
        monkeypatch.setattr(session_ready, "scrollback", lambda s, p=0: scrollback)
        return sent

    def test_unverified_but_landed_is_consumed(self, isolate, monkeypatch):
        from agentwire import session_ready

        msgs = inbox.enqueue("s", "PR drafted", kind="done", sender="w")
        sent = self._patch(monkeypatch, deliver=(False, "delivery_unverified"),
                           scrollback=msgs[0].render())
        landed = session_ready.scrollback("s")
        # The echo appears only once our paste lands (the false-negative case).
        monkeypatch.setattr(
            session_ready, "scrollback",
            lambda s, p=0: landed if sent else f"{RULE}\n❯\n{RULE}")
        res = inbox.flush_session("s")
        assert res["delivered"] == 1 and not res["deferred"]
        assert inbox.list_messages("s") == []  # consumed, not re-injected

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

    def test_identical_text_messages_dont_collide(self, isolate, monkeypatch):
        # Two byte-identical report-backs from one sender. Only the first is on
        # scrollback; the unique id token must keep the second distinct so it
        # isn't deduped to nothing.
        a = inbox.enqueue("orch", "PR drafted", kind="done", sender="worker")[0]
        inbox.enqueue("orch", "PR drafted", kind="done", sender="worker")
        self._patch(monkeypatch, deliver=(False, "delivery_unverified"),
                    scrollback=a.render())
        inbox.flush_session("orch")
        remaining = inbox.list_messages("orch")
        assert len(remaining) == 1, "the second identical message must survive"
        assert remaining[0].id != a.id and remaining[0].attempts == 1


class TestForceFlush:
    def test_force_pastes_despite_nonempty_box(self, isolate, monkeypatch):
        inbox.enqueue("s", "urgent", kind="done", sender="w")
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: "draft")
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
    def test_gc_batches_one_email_per_recipient(self, isolate, monkeypatch):
        # Regression (#829/#830): gc_sender must send ONE digest email per
        # recipient, not one per dead-lettered message — exercised against the
        # real send_email seam (not a stubbed _escalate_dead_letters), so a
        # regression back to per-message escalation inside the gc loop would
        # actually be caught here.
        sent = []
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **kw: sent.append(kw) or SimpleNamespace(success=True),
        )
        inbox.enqueue("orch", "PR drafted", kind="done", sender="worker")
        inbox.enqueue("orch", "need review", kind="request", sender="worker")
        res = inbox.gc_sender("worker")
        assert res["dead"] == 2
        assert len(sent) == 1
        assert "2 undelivered messages" in sent[0]["subject"]


# =============================================================================
# Session-name validation — path-traversal hardening
# =============================================================================


class TestSessionNameValidation:
    BAD = ["../../evil", "/tmp/evil", "a/../../b", "..", "", "a//b", "a b", "a\x00b", "~root"]

    def test_enqueue_traversal_raises_before_any_write(self, isolate, tmp_path):
        accepted = []
        for name in self.BAD:
            try:
                inbox.enqueue(name, "pwned", sender="attacker")
                accepted.append(name)
            except ValueError:
                pass
        assert not accepted, accepted
        assert not isolate.exists()  # nothing created at all
        assert not (tmp_path / "evil").exists()


def _enq(*args, pasted=True, **kw):
    """Enqueue and (by default) stamp a recent drain paste — heal provenance."""
    msgs = inbox.enqueue(*args, **kw)
    if pasted:
        for m in msgs:
            m.pasted_ts = inbox._now_ms()
            inbox._write_message(m.path, m)
    return msgs


class TestStuckInBox:
    """#689 — our own message sitting in the recipient's input box is a
    swallowed-Enter delivery, not a human draft: the drain must finish it with
    an Enter-only retry (never a re-paste) and unlink only once submitted."""

    def _patch(self, monkeypatch, box_content, finish_ok):
        from agentwire import session_ready, usage_limit
        monkeypatch.setattr(usage_limit, "_capture", lambda s: "dummy screen")
        monkeypatch.setattr(
            prompt_router, "input_box_content_sgr", lambda vis: box_content)
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
        # No pre-dedup hits: nothing on scrollback outside the box.
        monkeypatch.setattr(
            session_ready, "scrollback", lambda s, p=0: f"{RULE}\n❯\n{RULE}")
        finishes = []
        monkeypatch.setattr(
            session_ready, "heal_submit",
            lambda s, m, pane_index=0:
                (finishes.append(m) or (
                    "submitted" if finish_ok else "unconfirmed")))
        pasted = []
        monkeypatch.setattr(
            prompt_router, "safe_deliver",
            lambda s, p, text: (pasted.append(text) or (True, "delivered")))
        return finishes, pasted

    def test_stuck_message_finished_and_unlinked(self, isolate, monkeypatch):
        m = _enq("s", "PR drafted", kind="done", sender="w")[0]
        finishes, pasted = self._patch(monkeypatch, m.render(), finish_ok=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 1
        assert finishes == [m.render()]
        assert pasted == []  # NEVER re-pasted (#621)
        assert inbox.list_messages("s") == []


class TestBroadcastServiceExclusion:
    """#1038 — @all never fans out to infrastructure service sessions."""

    def test_at_all_excludes_services(self, isolate, monkeypatch):
        from agentwire import services
        monkeypatch.setattr(
            inbox, "_live_agent_sessions",
            lambda: ["a", "agentwire-notifications", "orch"])
        monkeypatch.setattr(
            services, "is_service_session",
            lambda s: s == "agentwire-notifications")
        written = _enq("@all", "team update", sender="orch")
        assert sorted(m.to for m in written) == ["a"]


class TestWindowedStuckHeal:
    """#1105 — a swallowed Enter on a message taller than the input box.

    Claude Code's box scrolls, so a ~400-char paste on a 30-column pane renders
    only its TAIL. The #689 heal used to demand the WHOLE render in the box, so
    the drain read its own landed paste as a human draft: ``box_not_empty``,
    penalized every tick, the text never submitted.
    """

    LONG = "word " * 90  # ~450 chars: far taller than a narrow pane's box

    def _tail(self, render: str, n: int = 150) -> str:
        return render[-n:]

    def _patch(self, monkeypatch, box_content, finish_ok=True):
        return TestStuckInBox()._patch(monkeypatch, box_content, finish_ok)

    def test_windowed_tail_is_healed_enter_only(self, isolate, monkeypatch):
        m = _enq("s", self.LONG, kind="request", sender="w")[0]
        finishes, pasted = self._patch(monkeypatch, self._tail(m.render()))
        res = inbox.flush_session("s")
        assert res["delivered"] == 1 and not res["deferred"]
        assert finishes == [m.render()]
        assert pasted == []  # never re-pasted on top of the landed text
        assert inbox.list_messages("s") == []


class TestDeferralVisibility:
    """#1105 — `msg inbox` shows why each pending message is waiting."""

    def test_inbox_line_names_reason_time_and_evidence(self, isolate, monkeypatch):
        from agentwire import msg_cli

        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=False, box="half typed thing")
        inbox.flush_session("s")
        (m,) = inbox.list_messages("s")
        line = msg_cli._deferral_line(m)
        assert line.startswith("box_not_empty at ") and "foreign_draft len=16" in line


class TestHealClobberAndBounds:
    """#1105 review: the heal presses Enter only into provably-our paste."""

    LONG = "word " * 90

    def _patch(self, monkeypatch, box_content, outcome="submitted"):
        return TestStuckInBox()._patch(
            monkeypatch, box_content, finish_ok=(outcome == "submitted"))

    def test_human_draft_quoting_our_tail_is_never_given_enter(
        self, isolate, monkeypatch
    ):
        # The review's repro_a shapes: a draft that quotes the last 80..120
        # chars of the render INCLUDING the id token. No paste record -> foreign.
        for n in (80, 81, 100, 120):
            m = _enq("s", self.LONG, kind="request", sender="w", pasted=False)[0]
            finishes, pasted = self._patch(monkeypatch, m.render()[-n:])
            res = inbox.flush_session("s")
            assert finishes == [] and pasted == [], n
            assert res["reason"] in {"box_not_empty", "box_static"}, n
            assert len(inbox.list_messages("s")) == 1, n
            inbox.purge_pending("s")

    def test_exhausted_heals_dead_letter_as_unconfirmed(self, isolate, monkeypatch):
        m = _enq("s", self.LONG, kind="request", sender="w")[0]
        self._patch(monkeypatch, m.render()[-150:], outcome="unconfirmed")
        monkeypatch.setattr(inbox, "_escalate_dead_letters", lambda *a, **k: None)
        for _ in range(inbox.MAX_HEALS - 1):
            inbox.flush_session("s")
            assert len(inbox.list_messages("s")) == 1  # still pending, no penalty
        inbox.flush_session("s")
        assert inbox.list_messages("s") == []
        (dead,) = inbox.list_dead("s")
        assert dead.reason == inbox._UNCONFIRMED_REASON
        assert dead.heals == inbox.MAX_HEALS

    def test_detail_never_contains_draft_text(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=False, box="my private half sentence")
        inbox.flush_session("s")
        (m,) = inbox.list_messages("s")
        events = inbox.EVENTS_FILE.read_text()
        for blob in (m.detail, json.dumps(m.to_dict()), events):
            assert "private" not in blob


class TestBusyWaitIsBoundedByAge:
    """#1113 — the busy defers (``queued_placeholder`` / ``box_static``) cost no
    attempts, so a placeholder that never clears used to hold an entry forever.
    Driven through ``flush_session`` with a stub clock (the drain's real call
    path): wait = age from the paste, or from enqueue if never pasted."""

    PLACEHOLDER = "Press up to edit queued messages"
    MINUTE = 60 * 1000

    def _run(self, monkeypatch, *, pasted, box, minutes, scrollback="", kind="request"):
        from agentwire import session_ready

        t0 = inbox._now_ms()
        now = [t0]
        monkeypatch.setattr(inbox, "_now_ms", lambda: now[0])
        escalated = []
        monkeypatch.setattr(
            inbox, "_escalate_dead_letters",
            lambda batch, r: batch and escalated.append(([m.kind for m in batch], r)))
        _enq("s", "report body", kind=kind, sender="w", pasted=pasted)
        _patch_delivery(monkeypatch, empty=False, box=box)
        monkeypatch.setattr(session_ready, "scrollback", lambda s, p=0: scrollback)
        state = {}
        for minute in minutes:
            now[0] = t0 + minute * self.MINUTE
            inbox.flush_session("s")
            state[minute] = (len(inbox.list_messages("s")), len(inbox.list_dead("s")))
        return state, escalated

    def test_a_placeholder_that_never_clears_dead_letters_at_the_bound(self, isolate, monkeypatch):
        """Never pasted, and pasted-then-placeholder, and an unknown static box."""
        bad = []
        for label, pasted, box in (
            ("never pasted", False, self.PLACEHOLDER),
            ("pasted, placeholder forever", True, self.PLACEHOLDER),
            ("pasted, static unknown box", True, "some static status widget"),
        ):
            inbox.purge_pending("s")
            minutes = [0, 1, 5, 11, 20, 39, 41, 42]
            state, escalated = self._run(monkeypatch, pasted=pasted, box=box, minutes=minutes)
            # alive and silent before the bound, dead (and escalated) after it
            if any(state[t] != (1, 0) for t in (0, 1, 5, 11, 20, 39)):
                bad.append((label, "dead-lettered before the bound", state))
            if state[41] != (0, 1):
                bad.append((label, "not dead-lettered past the bound", state))
            if escalated != [(["request"], inbox._UNCONFIRMED_REASON)]:
                bad.append((label, "no/odd owner escalation", escalated))
            inbox.purge_dead("s")
        assert not bad, bad


class TestHealEqualityAndStamp:
    """#1105 round 2: a box holding our message PLUS anything is not ours."""

    SHORT = "PR drafted"

    def _heal(self, monkeypatch, box):
        m = _enq("s", self.SHORT, kind="done", sender="w")[0]
        finishes, pasted = TestStuckInBox()._patch(
            monkeypatch, box(m.render()), finish_ok=True)
        res = inbox.flush_session("s")
        return m, finishes, pasted, res

    def test_mixed_boxes_are_never_given_enter(self, isolate, monkeypatch):
        shapes = {
            "appended": lambda r: r + " my reply",
            "prepended": lambda r: "re: " + r,
            "quoted+reply": lambda r: "> " + r + "\nreply",
            "middle": lambda r: r[:20] + " INSERTED " + r[20:],
            "trailing partial": lambda r: r + " th",
            "multi-line reply": lambda r: r + "\nline one\nline two\nline three",
        }
        bad = []
        for name, shape in shapes.items():
            m, finishes, pasted, _ = self._heal(monkeypatch, shape)
            if finishes or pasted or len(inbox.list_messages("s")) != 1:
                bad.append(name)
            inbox.purge_pending("s")
        assert not bad, bad

    def test_refused_paste_leaves_no_stamp(self, isolate, monkeypatch):
        # Only refusals that return BEFORE pasting. box_not_empty /
        # delivery_unverified may have landed windowed text we could not
        # recognise yet — those keep the stamp (#1164, TestUnprovenPaste…).
        stamped = {}
        for reason in sorted(inbox._NOT_PASTED_REASONS):
            inbox.enqueue("s", "hi", sender="x")
            _patch_delivery(monkeypatch, empty=True, deliver=(False, reason))
            inbox.flush_session("s")
            (m,) = inbox.list_messages("s")
            if m.pasted_ts:
                stamped[reason] = m.pasted_ts
            inbox.purge_pending("s")
        assert not stamped, stamped


class TestUnprovenPasteKeepsItsStamp:
    """#1164 — the drain's own paste, left in the box by a swallowed Enter, was
    re-read as a human's ``foreign_draft`` forever and then pasted again.

    On a narrow pane the box windows the paste to a short tail. The post-paste
    check (``_undelivered_reason``) could not match it, reported
    ``box_not_empty``, and the drain CLEARED its paste stamp — so the next tick
    saw an unstamped, unrecognised box and deferred for good.
    """

    MSG_TEXT = "Please review the collapse height change carefully " * 3

    def _drive(self, monkeypatch, deliver_reason):
        from agentwire import session_ready, usage_limit

        m = inbox.enqueue("s", self.MSG_TEXT, kind="request", sender="agentwire")[0]
        tail = m.render()[-81:]
        box = {"content": ""}
        monkeypatch.setattr(usage_limit, "_capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "dummy")
        monkeypatch.setattr(
            prompt_router, "input_box_content_sgr", lambda vis: box["content"])
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
        monkeypatch.setattr(
            session_ready, "scrollback", lambda s, p=0: f"{RULE}\n❯\n{RULE}")
        pastes, enters = [], []

        def deliver(s, p, text):
            pastes.append(text)
            box["content"] = tail  # the paste lands, windowed; Enter swallowed
            return False, deliver_reason

        def heal(s, text, pane_index=0):
            enters.append(text)
            box["content"] = ""
            return "submitted"

        monkeypatch.setattr(prompt_router, "safe_deliver", deliver)
        monkeypatch.setattr(session_ready, "heal_submit", heal)
        inbox.flush_session("s")  # tick 1: paste, Enter swallowed
        inbox.flush_session("s")  # tick 2: must heal, never re-paste
        inbox.flush_session("s")
        return pastes, enters

    def test_unproven_paste_is_healed_enter_only_and_consumed_once(
        self, isolate, monkeypatch
    ):
        bad = {}
        for reason in ("box_not_empty", "delivery_unverified", "pasted_unsubmitted"):
            for name in [p.name for p in inbox.pending_files("s")]:
                (inbox.session_dir("s") / name).unlink()
            pastes, enters = self._drive(monkeypatch, reason)
            if len(pastes) != 1 or len(enters) != 1 or inbox.list_messages("s"):
                bad[reason] = (len(pastes), len(enters), len(inbox.list_messages("s")))
        assert not bad, bad

    def test_unrecordable_stamp_means_no_paste(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        sent = _patch_delivery(monkeypatch, empty=True)

        def boom(path, msg):
            raise OSError("disk full")

        monkeypatch.setattr(inbox, "_write_message", boom)
        res = inbox.flush_session("s")
        assert sent == [] and res["delivered"] == 0
