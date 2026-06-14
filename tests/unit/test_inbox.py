"""Tests for the polite agent-to-agent inbox (#296).

Covers the inbox store (schema, atomic write, ordering, dead-letter), the
``prompt_is_empty`` collision detector against real capture-pane signatures,
and the flush drain (gating, batch coalescing, broadcast, attempt cap).
"""

import json

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


class TestTick:
    def test_tick_drains_all(self, isolate, monkeypatch):
        inbox.enqueue("a", "x", sender="z")
        inbox.enqueue("b", "y", sender="z")
        _patch_delivery(monkeypatch, empty=True)
        res = inbox.tick()
        assert len(res["flushed"]) == 2
        assert inbox.list_messages("a") == [] and inbox.list_messages("b") == []
