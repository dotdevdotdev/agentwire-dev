"""Tests for ``agentwire notify-parent`` — the --queued msg-inbox path (#667).

Idle report-backs ride the polite msg rail (kind=done): empty-box gate, busy
deferral without dead-letter penalty, full-line scrollback dedup, and
email-on-dead-letter. Direct-paste behavior for non-queued callers is
unchanged and covered by test_prompt_router's safe_deliver tests.
"""

from types import SimpleNamespace

import pytest

from agentwire import inbox, pane_manager, prompt_router
from agentwire.notify_cli import cmd_notify_parent


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    monkeypatch.setattr(inbox, "INBOX_ROOT", root)
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    return root


def _args(**kw):
    defaults = dict(
        text=["is", "idle", "and", "done", "working"],
        to=None, json=False, quiet=True, raw=False, on_idle=False, queued=True,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


@pytest.fixture
def worktree_child(monkeypatch):
    """A pane-0 worktree child whose parent resolves via creator metadata."""
    monkeypatch.setattr(pane_manager, "get_current_session", lambda: "agentwire-dev-issue-661-bar")
    monkeypatch.setattr(pane_manager, "get_current_pane_index", lambda: 0)
    monkeypatch.setattr(prompt_router, "resolve_parent", lambda s, p: ("orch", 0))


class TestQueuedMode:
    def test_enqueues_done_kind_to_resolved_parent(self, isolate, worktree_child):
        assert cmd_notify_parent(_args()) == 0
        msgs = inbox.list_messages("orch")
        assert len(msgs) == 1
        assert msgs[0].kind == "done"
        assert msgs[0].sender == "agentwire-dev-issue-661-bar"
        assert msgs[0].text == "is idle and done working"

    def test_no_direct_paste_in_queued_mode(self, isolate, worktree_child, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError("queued mode must never call safe_deliver")

        monkeypatch.setattr(prompt_router, "safe_deliver", boom)
        assert cmd_notify_parent(_args()) == 0

    def test_explicit_to_target(self, isolate, worktree_child):
        assert cmd_notify_parent(_args(to="boss")) == 0
        assert len(inbox.list_messages("boss")) == 1

    def test_enqueue_failure_is_surfaced_not_silent(self, isolate, worktree_child, monkeypatch):
        def fail(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(inbox, "enqueue", fail)
        assert cmd_notify_parent(_args()) == 1

    def test_no_parent_still_errors(self, isolate, monkeypatch):
        monkeypatch.setattr(pane_manager, "get_current_session", lambda: "solo")
        monkeypatch.setattr(pane_manager, "get_current_pane_index", lambda: 0)
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda s, p: None)
        assert cmd_notify_parent(_args()) == 1
        assert inbox.list_messages("solo") == []

    def test_on_idle_service_session_skips(self, isolate, worktree_child, monkeypatch):
        from agentwire import services

        monkeypatch.setattr(services, "is_service_session", lambda s: True)
        assert cmd_notify_parent(_args(on_idle=True)) == 0
        assert inbox.list_messages("orch") == []


class TestQueuedRendersUniquely:
    def test_rendered_lines_are_distinct_for_same_prefix_senders(self, isolate, monkeypatch):
        """Two worktree children with a shared long name prefix and identical
        text must produce distinct rendered lines (the ⟨#id6⟩ tail), so the
        full-line dedup can never cross-match them (#621/#667)."""
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda s, p: ("orch", 0))
        monkeypatch.setattr(pane_manager, "get_current_pane_index", lambda: 0)
        for name in ("agentwire-dev-issue-659-a", "agentwire-dev-issue-659-b"):
            monkeypatch.setattr(pane_manager, "get_current_session", lambda n=name: n)
            assert cmd_notify_parent(_args()) == 0
        rendered = [m.render() for m in inbox.list_messages("orch")]
        assert len(rendered) == 2 and rendered[0] != rendered[1]
