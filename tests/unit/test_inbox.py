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

SGR_EMPTY_BOX = f"""\
some agent output above
{DIM_RULE}
❯
{DIM_RULE}
  status bar
"""

GHOST_BOX = f"""\
some agent output above
{DIM_RULE}
❯ \x1b[2mTry "fix lint errors"\x1b[22m
{DIM_RULE}
  status bar
"""

# Ghost styled with a combined dim+color sequence and reset via ESC[0m.
GHOST_BOX_COMBINED = f"""\
output
{DIM_RULE}
❯ \x1b[2;38;5;242mhow can I help?\x1b[0m
{DIM_RULE}
  status bar
"""

SGR_DRAFT_BOX = f"""\
output
{DIM_RULE}
❯ real typed draft
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

SGR_QUEUED_BOX = f"""\
output
{DIM_RULE}
❯ \x1b[2mPress up to edit queued messages\x1b[22m
{DIM_RULE}
  status bar
"""


class TestInputBoxSgr:
    def test_sgr_empty_box(self):
        assert prompt_router.input_box_content_sgr(SGR_EMPTY_BOX) == ""

    def test_dim_only_ghost_reads_empty(self):
        assert prompt_router.input_box_content_sgr(GHOST_BOX) == ""

    def test_combined_dim_color_ghost_reads_empty(self):
        assert prompt_router.input_box_content_sgr(GHOST_BOX_COMBINED) == ""

    def test_real_draft_still_defers(self):
        assert prompt_router.input_box_content_sgr(SGR_DRAFT_BOX) == "real typed draft"

    def test_mixed_dim_and_typed_keeps_typed(self):
        content = prompt_router.input_box_content_sgr(MIXED_BOX)
        assert content == "typed prefix"

    def test_dim_queued_placeholder_reads_empty(self):
        # If a future build renders the queued placeholder dim it's ghost text;
        # today's non-dim render still routes through is_queued_placeholder.
        assert prompt_router.input_box_content_sgr(SGR_QUEUED_BOX) == ""

    def test_plain_queued_placeholder_unchanged(self):
        assert (
            prompt_router.input_box_content_sgr(QUEUED_BOX)
            == "Press up to edit queued messages"
        )

    def test_plain_captures_fall_back(self):
        # No SGR at all — behaves exactly like the plain parse.
        assert prompt_router.input_box_content_sgr(EMPTY_BOX) == ""
        assert prompt_router.input_box_content_sgr(DRAFT_BOX).startswith("this is my")
        assert prompt_router.input_box_content_sgr(DIALOG) is None

    def test_dim_prompt_glyph_falls_back_to_plain(self):
        # If the glyph itself renders dim the SGR parse can't find the box —
        # degrade to the plain (conservative, defer-shaped) result.
        weird = f"output\n{RULE}\n\x1b[2m❯ hint text\x1b[22m\n{RULE}\n status"
        assert prompt_router.input_box_content_sgr(weird) == "hint text"


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
        assert msg.render() == f"[MSG from worker · done] PR drafted  ⟨#{msg.short_id()}⟩"
        assert msg.render().startswith("[MSG from worker · done] PR drafted")

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

    def test_defers_when_safe_deliver_refuses(self, isolate, monkeypatch):
        # A penalized refusal (the pane runs a shell — pasted text could
        # EXECUTE, and nothing about that is self-clearing). The other refusal,
        # target_parked, defers WITHOUT penalty — see TestParkedDefer (#872).
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=True, deliver=(False, "target_not_agent"))
        res = inbox.flush_session("s")
        assert res["deferred"] and res["reason"] == "target_not_agent"
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

    def test_three_messages_still_one_paste(self, isolate, monkeypatch):
        # The other side of the #930 line bound: 3 messages measured as fully
        # healable (3 of 3 stuck hits at 386 chars) must stay a SINGLE paste.
        for i in range(3):
            inbox.enqueue("s", f"m{i}", sender="x")
        sent = _patch_delivery(monkeypatch, empty=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 3
        assert len(sent) == 1

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

    def test_small_pair_stays_one_paste(self, isolate, monkeypatch):
        # Both-sides for the char bound: a pair comfortably under it coalesces.
        inbox.enqueue("s", "short one", sender="x")
        inbox.enqueue("s", "short two", sender="x")
        sent = _patch_delivery(monkeypatch, empty=True)
        inbox.flush_session("s")
        assert len(sent) == 1

    def test_oversized_single_message_pastes_alone(self, isolate, monkeypatch):
        # A single message over the char bound can't be split — it goes alone
        # (never starves the queue), and its neighbors go in their own paste.
        inbox.enqueue("s", "x" * 600, sender="x")
        inbox.enqueue("s", "tail", sender="x")
        sent = _patch_delivery(monkeypatch, empty=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 2
        assert len(sent) == 2
        assert "x" * 600 in sent[0] and "tail" in sent[1]

    def test_batch_failure_defers_only_attempted_batch(self, isolate, monkeypatch):
        # A refusal mid-drain penalizes only the batch that was actually
        # pasted-at; the un-attempted tail stays pending with attempts == 0.
        for i in range(5):
            inbox.enqueue("s", f"r{i}", sender="x")
        _patch_delivery(monkeypatch, empty=True, deliver=(False, "target_not_agent"))
        res = inbox.flush_session("s")
        assert res["deferred"] and res["reason"] == "target_not_agent"
        msgs = inbox.list_messages("s")
        assert len(msgs) == 5
        assert [m.attempts for m in msgs] == [1, 1, 1, 0, 0]

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
        # target_not_agent, not target_parked: parked is penalty-free (#872) and
        # so can never reach the cap.
        inbox.enqueue("s", "stuck", sender="x")
        _patch_delivery(monkeypatch, empty=True, deliver=(False, "target_not_agent"))
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        dead = inbox.list_dead("s")
        assert len(dead) == 1 and dead[0].reason == "target_not_agent"

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
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: None)
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)

        res = inbox.flush_session("s")
        assert res["deferred"]
        assert res["reason"] == "target_busy"
        # attempts must NOT increment for target_busy
        assert inbox.list_messages("s")[0].attempts == 0

    def test_done_under_threshold_defers_box_not_empty(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: "draft content")
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

        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: "draft content")
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

        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: None)
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

        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: "draft content")
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
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(
            prompt_router, "input_box_content_sgr",
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

    def test_parked_defers_without_penalty(self, isolate, park):
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        park.on("s")
        res = inbox.flush_session("s")
        assert res["deferred"] and res["reason"] == "target_parked"
        assert park.sent == []  # never pasted into a parked session
        assert inbox.list_messages("s")[0].attempts == 0

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

    def test_gone_still_dies_fast_while_parked_waits(self, isolate, park, monkeypatch):
        """The distinction the fix preserves: parked defers forever, gone doesn't.

        ``target_gone`` is checked before the park gate and keeps its own fast
        GONE_MAX_ATTEMPTS cap (#694) — making parked penalty-free must not make
        a positively-absent recipient immortal too.
        """
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        park.on("s")
        monkeypatch.setattr(inbox, "live_sessions", lambda: set())  # s is gone
        for _ in range(inbox.GONE_MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert inbox.list_messages("s") == []
        dead = inbox.list_dead("s")
        assert len(dead) == 1 and dead[0].reason == "target_gone"


class TestBoxStatic:
    """Unrecognized-but-static box content (#669): identical across N sweeps →
    defer WITHOUT penalty (like target_busy) instead of burning to dead-letter."""

    def test_static_content_stops_penalizing(self, isolate, monkeypatch):
        inbox.enqueue("s", "PR done", kind="done", sender="worker")
        sent = _patch_delivery(monkeypatch, empty=False, box='Try "fix lint errors"')
        reasons = [
            inbox.flush_session("s")["reason"]
            for _ in range(inbox.MAX_ATTEMPTS + 5)
        ]
        # Pre-threshold sweeps penalize; from the Nth identical capture on, no penalty.
        assert reasons[: inbox._BOX_STATIC_THRESHOLD - 1] == ["box_not_empty"] * (
            inbox._BOX_STATIC_THRESHOLD - 1
        )
        assert set(reasons[inbox._BOX_STATIC_THRESHOLD - 1:]) == {"box_static"}
        pending = inbox.list_messages("s")
        assert len(pending) == 1
        assert pending[0].attempts == inbox._BOX_STATIC_THRESHOLD - 1
        assert inbox.list_dead("s") == []  # never dead-lettered
        assert sent == []  # and never pasted

    def test_changing_content_keeps_penalizing(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=False)  # default: varies per sweep
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert inbox.list_messages("s") == []
        assert len(inbox.list_dead("s")) == 1

    def test_box_static_counter_unit(self, isolate):
        assert not inbox._box_static("s", "x")
        assert not inbox._box_static("s", "x")
        assert inbox._box_static("s", "x")  # third identical sweep
        assert not inbox._box_static("s", "y")  # content change resets
        inbox._clear_box_state("s")
        assert not inbox._box_static("s", "y")
        assert not inbox._box_static("s", "y")
        assert inbox._box_static("s", "y")

    def test_state_file_is_not_a_message(self, isolate):
        inbox._box_static("s", "x")
        assert inbox.list_messages("s") == []

    def test_empty_box_clears_state(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        inbox._box_static("s", "stale")
        _patch_delivery(monkeypatch, empty=True)
        inbox.flush_session("s")
        assert not inbox._box_state_path("s").exists()


class TestTargetGone:
    """#694: a recipient that positively doesn't exist defers as `target_gone`
    — a penalized reason with its own fast cap (GONE_MAX_ATTEMPTS ≈ minutes,
    not the 40-minute busy window) — checked BEFORE the box gate. The original
    hole: capturing a gone session parses as "no box" → `target_busy`, a
    no-penalty defer, so a done to a stale parent sat queued ~24h instead of
    burning out and escalating."""

    def test_gone_target_penalizes_not_target_busy(self, isolate, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"someone-else"})
        # Old-bug conditions: the gone session's capture yields no parseable box.
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "")
        monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "")
        inbox.enqueue("ghost", "hi", sender="w")
        res = inbox.flush_session("ghost")
        assert res["deferred"] and res["reason"] == "target_gone"
        msg = inbox.list_messages("ghost")[0]
        assert msg.attempts == 1 and msg.gone_attempts == 1

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

    def test_prior_busy_penalties_do_not_erode_grace(self, isolate, monkeypatch):
        # attempts accrued while the target lived must not insta-kill on gone:
        # the gone window is its own counter.
        m = inbox.enqueue("ghost", "hi", kind="done", sender="w")[0]
        m.attempts = inbox.GONE_MAX_ATTEMPTS + 3  # over the gone cap, under MAX
        inbox._write_message(m.path, m)
        monkeypatch.setattr(inbox, "live_sessions", lambda: set())
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **kw: SimpleNamespace(success=True),
        )
        for _ in range(inbox.GONE_MAX_ATTEMPTS - 1):
            inbox.flush_session("ghost")
            assert len(inbox.list_messages("ghost")) == 1  # full grace granted
        inbox.flush_session("ghost")
        assert len(inbox.list_dead("ghost")) == 1

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

    def test_reappeared_target_delivers(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="w")
        monkeypatch.setattr(inbox, "live_sessions", lambda: set())
        inbox.flush_session("s")  # one gone tick
        assert inbox.list_messages("s")[0].gone_attempts == 1
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"s"})
        sent = _patch_delivery(monkeypatch, empty=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 1 and len(sent) == 1
        assert inbox.list_messages("s") == []

    def test_gone_dead_letter_emails_owner_for_done(self, isolate, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: set())
        emails = []
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **kw: emails.append(kw) or SimpleNamespace(success=True),
        )
        inbox.enqueue("ghost", "PR #1 drafted", kind="done", sender="w")
        for _ in range(inbox.GONE_MAX_ATTEMPTS):
            inbox.flush_session("ghost")
        assert len(emails) == 1
        assert "target_gone" in emails[0]["body"]
        assert len(inbox.list_dead("ghost")) == 1


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
        # Both dead-letter in the same drain pass (same recipient) — one
        # digest email covers the batch, not one email per kind (#836).
        inbox.enqueue("s", "need creds", kind="request", sender="w")
        inbox.enqueue("s", "stuck!", kind="escalation", sender="w")
        self._occupied_agent(monkeypatch)
        sent = []
        self._capture_email(monkeypatch, sent)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert len(sent) == 1
        assert "2 undelivered messages" in sent[0]["subject"]
        assert "request" in sent[0]["body"] and "escalation" in sent[0]["body"]

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

    def test_large_batch_sends_one_digest_not_one_per_message(self, isolate, monkeypatch):
        # Regression (2026-07-19): a recipient stuck permanently undeliverable
        # (e.g. wrongly parented to a service session) can accumulate a large
        # backlog that all crosses MAX_ATTEMPTS in the same drain pass. That
        # must fire ONE digest email, not one per message (147 individual
        # emails in ~2s was the real incident).
        for i in range(20):
            inbox.enqueue("s", f"report {i}", kind="done", sender="w")
        self._occupied_agent(monkeypatch)
        sent = []
        self._capture_email(monkeypatch, sent)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert len(inbox.list_dead("s")) == 20
        assert len(sent) == 1
        assert "20 undelivered messages" in sent[0]["subject"]

    def _msg(self, i):
        return inbox.Message(
            id=f"id{i}", sender="w", to="s", kind="done", text=f"report {i}",
            ts=1000 + i, dead_ts=2000 + i,
        )

    def test_digest_detail_cap_boundary(self, isolate, monkeypatch):
        # Exactly at the cap: every message gets detail, no truncation line.
        sent = []
        self._capture_email(monkeypatch, sent)
        batch = [self._msg(i) for i in range(inbox._ESCALATE_DIGEST_DETAIL_CAP)]
        inbox._escalate_dead_letters(batch, "target_gone")
        assert len(sent) == 1
        assert "...and" not in sent[0]["body"]
        assert all(f"report {i}" in sent[0]["body"] for i in range(len(batch)))

    def test_digest_detail_cap_truncates_beyond_boundary(self, isolate, monkeypatch):
        # One over the cap: detail for the first CAP, a single "...and 1 more."
        sent = []
        self._capture_email(monkeypatch, sent)
        batch = [self._msg(i) for i in range(inbox._ESCALATE_DIGEST_DETAIL_CAP + 1)]
        inbox._escalate_dead_letters(batch, "target_gone")
        assert len(sent) == 1
        assert "...and 1 more." in sent[0]["body"]
        assert f"report {inbox._ESCALATE_DIGEST_DETAIL_CAP}" not in sent[0]["body"]


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

    def test_prefix_text_does_not_substring_collide(self, isolate, monkeypatch):
        # A.text is a strict prefix of B.text (same sender + kind). B is on
        # scrollback; A's full rendered line would be a substring of B's WITHOUT
        # the unique id token. The token must keep A from being consumed.
        sender = "agentwire-dev-fix-621-inbox"
        inbox.enqueue("orch", "done: phase 1", kind="done", sender=sender)
        b = inbox.enqueue("orch", "done: phase 1 and 2 complete", kind="done", sender=sender)[0]
        self._patch(monkeypatch, deliver=(False, "delivery_unverified"),
                    scrollback=b.render())  # only the LONGER message is visible
        inbox.flush_session("orch")
        remaining = inbox.list_messages("orch")
        assert len(remaining) == 1, "the prefix message must NOT be substring-consumed"
        assert remaining[0].text == "done: phase 1"
        assert remaining[0].attempts == 1

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
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: "draft")
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
    def test_gc_dead_letters_load_bearing(self, isolate, monkeypatch):
        emailed = []
        monkeypatch.setattr(inbox, "_escalate_dead_letters",
                            lambda batch, r: emailed.extend((m.kind, r) for m in batch))
        inbox.enqueue("orch", "PR drafted", kind="done", sender="worker")
        inbox.enqueue("orch", "need review", kind="request", sender="worker")
        res = inbox.gc_sender("worker")
        assert res["dead"] == 2 and res["dropped"] == 0
        assert inbox.list_messages("orch") == []
        assert len(inbox.list_dead("orch")) == 2
        assert emailed and all(r == "sender_exited" for _, r in emailed)

    def test_gc_drops_non_load_bearing(self, isolate, monkeypatch):
        monkeypatch.setattr(inbox, "_escalate_dead_letters", lambda batch, r: None)
        inbox.enqueue("orch", "fyi", kind="note", sender="worker")
        res = inbox.gc_sender("worker")
        assert res["dropped"] == 1 and res["dead"] == 0
        assert inbox.list_messages("orch") == []
        assert inbox.list_dead("orch") == []

    def test_gc_skips_recipient_with_held_lock(self, isolate, monkeypatch):
        # A flush draining this inbox holds the per-session lock; gc must NOT
        # dead-letter (and email) a message that flush is mid-delivery on.
        emailed = []
        monkeypatch.setattr(inbox, "_escalate_dead_letters",
                            lambda batch, r: emailed.extend(m.id for m in batch))
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
    BAD = ["../../x", "/etc/x", "a/../../b", "..", "a/..", "", "a//b", "a/", "/a",
           "a b", "a\x00b", "~root"]

    @pytest.mark.parametrize("name", BAD)
    def test_session_dir_rejects_traversal(self, isolate, name):
        with pytest.raises(ValueError):
            inbox.session_dir(name)
        assert not isolate.exists() or all(
            p.resolve().is_relative_to(isolate.resolve()) for p in isolate.rglob("*")
        )

    @pytest.mark.parametrize("name", BAD)
    def test_helpers_reject_traversal(self, isolate, name):
        for fn in (inbox.dead_dir, inbox.ingest_dir, inbox.pending_files,
                   inbox.list_messages, inbox.list_dead, inbox.list_ingest,
                   inbox.purge_pending):
            with pytest.raises(ValueError):
                fn(name)

    @pytest.mark.parametrize("name", ["orchestrator", "myproj-fix", "proj/child",
                                      "a.b_c@host-1", "p/c/grandchild"])
    def test_valid_names_accepted(self, isolate, name):
        assert inbox.session_dir(name) == isolate / name

    def test_enqueue_traversal_raises_before_any_write(self, isolate, tmp_path):
        with pytest.raises(ValueError):
            inbox.enqueue("../../evil", "pwned", sender="attacker")
        assert not isolate.exists()  # nothing created at all
        assert not (tmp_path / "evil").exists()

    def test_enqueue_absolute_path_raises(self, isolate):
        with pytest.raises(ValueError):
            inbox.enqueue("/tmp/evil", "pwned", sender="attacker")
        assert not isolate.exists()

    def test_nested_worktree_delivery_still_works(self, isolate):
        msgs = inbox.enqueue("proj/child", "hello", sender="orch")
        assert len(msgs) == 1
        assert msgs[0].path.is_relative_to(isolate / "proj" / "child")
        assert [m.text for m in inbox.list_messages("proj/child")] == ["hello"]


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

    def test_finish_failure_defers_without_penalty(self, isolate, monkeypatch):
        m = _enq("s", "PR drafted", kind="done", sender="w")[0]
        finishes, pasted = self._patch(monkeypatch, m.render(), finish_ok=False)
        res = inbox.flush_session("s")
        assert res["deferred"] and res["reason"] == "stuck_in_box"
        assert pasted == []
        msgs = inbox.list_messages("s")
        assert len(msgs) == 1
        assert msgs[0].attempts == 0  # no dead-letter penalty — target isn't refusing

    def test_foreign_draft_is_not_stuck(self, isolate, monkeypatch):
        # Box holds a human draft, not our message: normal defer path, no Enter.
        _enq("s", "PR drafted", kind="done", sender="w")
        finishes, pasted = self._patch(
            monkeypatch, "half-typed human thought", finish_ok=True)
        res = inbox.flush_session("s")
        assert res["deferred"]
        assert finishes == []  # never pressed Enter into a foreign draft
        assert len(inbox.list_messages("s")) == 1

    def test_partial_stuck_unlinks_only_stuck(self, isolate, monkeypatch):
        a = _enq("s", "alpha", kind="done", sender="w")[0]
        _enq("s", "beta", kind="done", sender="w")
        finishes, pasted = self._patch(monkeypatch, a.render(), finish_ok=True)
        res = inbox.flush_session("s")
        assert res["delivered"] == 1 and res["deferred"]
        remaining = inbox.list_messages("s")
        assert len(remaining) == 1 and "beta" in remaining[0].text
        assert pasted == []


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

    def test_service_still_reachable_by_name(self, isolate, monkeypatch):
        monkeypatch.setattr(inbox, "_live_agent_sessions", lambda: ["a"])
        written = _enq("agentwire-notifications", "direct", sender="orch")
        assert [m.to for m in written] == ["agentwire-notifications"]


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

    def test_foreign_long_draft_is_not_healed(self, isolate, monkeypatch):
        # Clobber protection: a long human draft that is not a tail of our
        # paste must never get an Enter pressed into it.
        _enq("s", self.LONG, kind="request", sender="w")
        finishes, pasted = self._patch(
            monkeypatch, "I was in the middle of writing a long thought " * 4)
        res = inbox.flush_session("s")
        assert res["deferred"] and res["reason"] in {"box_not_empty", "box_static"}
        assert finishes == [] and pasted == []
        assert len(inbox.list_messages("s")) == 1

    def test_text_inside_our_message_but_not_its_tail_is_not_healed(
        self, isolate, monkeypatch
    ):
        m = _enq("s", self.LONG, kind="request", sender="w")[0]
        head = m.render()[:150]  # a fragment of ours, but not what a paste leaves
        finishes, pasted = self._patch(monkeypatch, head)
        inbox.flush_session("s")
        assert finishes == [] and pasted == []

    def test_window_of_second_message_does_not_unlink_the_first(
        self, isolate, monkeypatch
    ):
        a = _enq("s", "alpha " * 5, kind="done", sender="w")[0]
        b = _enq("s", self.LONG, kind="done", sender="w")[0]
        batch = "\n".join([a.render(), b.render()])
        finishes, pasted = self._patch(monkeypatch, self._tail(batch))
        res = inbox.flush_session("s")
        assert res["delivered"] == 2  # both were in the one landed paste
        assert finishes == [batch] and pasted == []

    def test_window_of_first_message_leaves_the_second_pending(
        self, isolate, monkeypatch
    ):
        a = _enq("s", self.LONG, kind="done", sender="w")[0]
        _enq("s", "beta", kind="done", sender="w")
        finishes, pasted = self._patch(monkeypatch, self._tail(a.render()))
        res = inbox.flush_session("s")
        assert res["delivered"] == 1
        assert [m.text for m in inbox.list_messages("s")] == ["beta"]
        assert pasted == []


class TestDeferralVisibility:
    """#1105 — `msg inbox` shows why each pending message is waiting."""

    def test_reason_time_and_evidence_are_stamped(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=False, box="half typed thing")
        inbox.flush_session("s")
        (m,) = inbox.list_messages("s")
        assert m.reason == "box_not_empty"
        assert m.reason_ts > 0
        assert m.detail == "foreign_draft len=16"

    def test_no_penalty_deferral_is_stamped_too(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=True,
                        deliver=(False, "pasted_unsubmitted"))
        res = inbox.flush_session("s")
        (m,) = inbox.list_messages("s")
        assert res["reason"] == "pasted_unsubmitted"
        assert m.attempts == 0 and m.reason == "pasted_unsubmitted"
        assert m.reason_ts > 0
        assert inbox.list_dead("s") == []

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

    def test_stale_paste_record_is_foreign(self, isolate, monkeypatch):
        m = _enq("s", self.LONG, kind="request", sender="w")[0]
        m.pasted_ts = inbox._now_ms() - inbox.PASTE_HEAL_WINDOW_MS - 1000
        inbox._write_message(m.path, m)
        finishes, _ = self._patch(monkeypatch, m.render()[-120:])
        inbox.flush_session("s")
        assert finishes == []

    def test_drain_stamps_the_paste_before_pasting(self, isolate, monkeypatch):
        m = _enq("s", "hi", sender="x", pasted=False)[0]
        seen = {}

        def deliver(s, p, text):
            (cur,) = inbox.list_messages("s")
            seen["pasted_ts"] = cur.pasted_ts
            return (True, "delivered")

        _patch_delivery(monkeypatch, empty=True)
        monkeypatch.setattr(prompt_router, "safe_deliver", deliver)
        inbox.flush_session("s")
        assert seen["pasted_ts"] > 0 and m.pasted_ts == 0

    def test_box_changed_leaves_entry_pending_and_not_unlinked(
        self, isolate, monkeypatch
    ):
        from agentwire import session_ready

        m = _enq("s", self.LONG, kind="request", sender="w")[0]
        self._patch(monkeypatch, m.render()[-150:])
        monkeypatch.setattr(
            session_ready, "heal_submit", lambda s, t, pane_index=0: "box_changed")
        res = inbox.flush_session("s")
        assert res["reason"] == "box_changed" and res["deferred"]
        (left,) = inbox.list_messages("s")
        assert left.reason == "box_changed" and left.attempts == 1

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
        assert dead.reason == "delivery could not be confirmed"
        assert dead.heals == inbox.MAX_HEALS

    def test_exhaustion_email_says_unconfirmed_not_lost(self, isolate, monkeypatch):
        m = _enq("s", self.LONG, kind="request", sender="w")[0]
        self._patch(monkeypatch, m.render()[-150:], outcome="unconfirmed")
        reasons = []
        monkeypatch.setattr(
            inbox, "_escalate_dead_letters", lambda batch, r: reasons.append(r))
        for _ in range(inbox.MAX_HEALS):
            inbox.flush_session("s")
        assert reasons[-1] == "delivery could not be confirmed"

    def _verdict(self, monkeypatch, scrollback, box_sgr):
        from agentwire import session_ready

        m = _enq("s", self.LONG, kind="request", sender="w")[0]
        monkeypatch.setattr(session_ready, "scrollback", lambda s, p=0: scrollback(m))
        monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "x")
        monkeypatch.setattr(
            prompt_router, "input_box_content_sgr", lambda vis: box_sgr)
        return inbox._exhaustion_verdict(m)

    def test_verdict_delivered_when_echoed_outside_the_box(self, isolate, monkeypatch):
        v = self._verdict(
            monkeypatch,
            lambda m: f"❯ {m.render()[:60]} … {m.render()[-30:]}\n{RULE}\n❯\n{RULE}",
            "")
        assert v == "delivered"

    def test_verdict_queued_when_agent_shows_the_queue_placeholder(
        self, isolate, monkeypatch
    ):
        v = self._verdict(
            monkeypatch, lambda m: f"{RULE}\n❯\n{RULE}",
            "Press up to edit queued messages")
        assert v == "queued"

    def test_verdict_unconfirmed_when_nothing_observable(self, isolate, monkeypatch):
        v = self._verdict(monkeypatch, lambda m: f"{RULE}\n❯\n{RULE}", "")
        assert v == "unconfirmed"

    def test_delivered_verdict_unlinks_instead_of_dead_lettering(
        self, isolate, monkeypatch
    ):
        m = _enq("s", self.LONG, kind="request", sender="w")[0]
        m.heals = inbox.MAX_HEALS - 1
        inbox._write_message(m.path, m)
        monkeypatch.setattr(inbox, "_exhaustion_verdict", lambda msg: "delivered")
        dead = inbox._bump_attempts(inbox.list_messages("s"), "stuck_in_box")
        assert dead == 0 and inbox.list_messages("s") == []
        assert inbox.list_dead("s") == []

    def test_queued_verdict_keeps_the_entry_pending(self, isolate, monkeypatch):
        m = _enq("s", self.LONG, kind="request", sender="w")[0]
        m.heals = inbox.MAX_HEALS - 1
        inbox._write_message(m.path, m)
        monkeypatch.setattr(inbox, "_exhaustion_verdict", lambda msg: "queued")
        assert inbox._bump_attempts(inbox.list_messages("s"), "stuck_in_box") == 0
        assert len(inbox.list_messages("s")) == 1 and inbox.list_dead("s") == []

    def test_queued_wait_is_bounded_by_the_paste_age(self, isolate, monkeypatch):
        """#1113: a placeholder that never clears must not hold a message in
        silence. Recent paste + placeholder keeps waiting; past QUEUED_WAIT_MS it
        dead-letters as unconfirmed and escalates (load-bearing kind)."""
        monkeypatch.setattr(inbox, "_exhaustion_verdict", lambda msg: "queued")
        escalated = []
        monkeypatch.setattr(
            inbox, "_escalate_dead_letters", lambda batch, r: batch and escalated.append((len(batch), r)))
        outcomes = {}
        for label, age_ms in (("recent", 5 * 60 * 1000),
                              ("just_inside", inbox.QUEUED_WAIT_MS - 60 * 1000),
                              ("old", inbox.QUEUED_WAIT_MS + 1000)):
            to = f"s-{label}"
            m = _enq(to, self.LONG, kind="request", sender="w")[0]
            m.heals = inbox.MAX_HEALS - 1
            m.pasted_ts = inbox._now_ms() - age_ms
            inbox._write_message(m.path, m)
            inbox._bump_attempts(inbox.list_messages(to), "stuck_in_box")
            outcomes[label] = (len(inbox.list_messages(to)), len(inbox.list_dead(to)))
        assert outcomes == {
            "recent": (1, 0), "just_inside": (1, 0), "old": (0, 1)}, outcomes
        assert escalated == [(1, "delivery could not be confirmed")], escalated
        (dead,) = inbox.list_dead("s-old")
        assert dead.reason == "delivery could not be confirmed"

    def test_busy_reasons_stay_unbounded(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=True, deliver=(False, "queued_placeholder"))
        for _ in range(inbox.MAX_HEALS + 5):
            inbox.flush_session("s")
        assert len(inbox.list_messages("s")) == 1 and inbox.list_dead("s") == []

    def test_detail_never_contains_draft_text(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=False, box="my private half sentence")
        inbox.flush_session("s")
        (m,) = inbox.list_messages("s")
        events = inbox.EVENTS_FILE.read_text()
        for blob in (m.detail, json.dumps(m.to_dict()), events):
            assert "private" not in blob


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

    def test_trailing_whitespace_only_is_still_ours(self, isolate, monkeypatch):
        m, finishes, pasted, res = self._heal(monkeypatch, lambda r: r + "  \n ")
        assert res["delivered"] == 1 and finishes == [m.render()] and not pasted

    def test_refused_paste_leaves_no_stamp(self, isolate, monkeypatch):
        for reason in ("target_dialog", "box_not_empty", "delivery_unverified"):
            inbox.enqueue("s", "hi", sender="x")
            _patch_delivery(monkeypatch, empty=True, deliver=(False, reason))
            inbox.flush_session("s")
            (m,) = inbox.list_messages("s")
            assert m.pasted_ts == 0, reason
            inbox.purge_pending("s")

    def test_landed_unsubmitted_paste_keeps_its_stamp(self, isolate, monkeypatch):
        inbox.enqueue("s", "hi", sender="x")
        _patch_delivery(monkeypatch, empty=True, deliver=(False, "pasted_unsubmitted"))
        inbox.flush_session("s")
        (m,) = inbox.list_messages("s")
        assert m.pasted_ts > 0

    def test_box_changed_clears_the_stamp(self, isolate, monkeypatch):
        from agentwire import session_ready

        m = _enq("s", self.SHORT, kind="done", sender="w")[0]
        TestStuckInBox()._patch(monkeypatch, m.render(), finish_ok=True)
        monkeypatch.setattr(
            session_ready, "heal_submit", lambda s, t, pane_index=0: "box_changed")
        inbox.flush_session("s")
        (left,) = inbox.list_messages("s")
        assert left.pasted_ts == 0
