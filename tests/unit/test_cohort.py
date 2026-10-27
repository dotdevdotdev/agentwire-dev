"""Fan-out cohort ledger (#852).

A parent that fans out children went idle while *waiting*; the idle handler
read idle as done, reaped the parent mid-fan-out, and the children leaked while
their report-backs dead-lettered into owner email. These cover the ledger the
three consumers share: the join primitive, the idle-handler guard, and the
watchdog sweeper.
"""

from types import SimpleNamespace

import pytest

from agentwire import cohort, inbox


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    """Point cohorts + inbox at throwaway dirs, and stub tmux out entirely."""
    monkeypatch.setattr(cohort, "COHORT_ROOT", tmp_path / "cohorts")
    monkeypatch.setattr(cohort, "EVENTS_FILE", tmp_path / "cohort-events.jsonl")
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)

    state = {"live": set(), "killed": []}

    def exists(session):
        return session in state["live"]

    def kill(session):
        state["killed"].append(session)
        state["live"].discard(session)
        return True

    agents = {"memory-manager", "orchestrator"}
    state["agents"] = agents
    monkeypatch.setattr(cohort, "is_agent_session", lambda s: s in agents)
    monkeypatch.setattr(cohort, "session_exists", exists)
    monkeypatch.setattr(cohort, "_kill", kill)
    monkeypatch.setattr(cohort.time, "sleep", lambda _: None)
    return state


def _fan_out(state, parent="memory-manager", n=2, ttl=cohort.DEFAULT_TTL):
    children = [f"memrev-child{i}" for i in range(n)]
    state["live"].add(parent)
    for child in children:
        state["live"].add(child)
        cohort.enroll(parent, child, task="memory-manager", ttl=ttl)
    return children


class TestCollect:
    def _report(self, parent, child, text="done: 3 memories pruned"):
        inbox.enqueue(parent, text, kind="done", sender=child)

    def test_report_is_consumed_before_the_kill(self, isolate, monkeypatch):
        # Ordering is load-bearing: `agentwire kill` runs inbox.gc_sender(),
        # which dead-letters the killed session's pending outbound AND emails
        # the owner. Kill-before-collect turns every child's `done` report into
        # owner email for work that succeeded.
        children = _fan_out(isolate, n=1)
        self._report("memory-manager", children[0])
        seen = {}

        def kill(session):
            seen[session] = [m.sender for m in inbox.list_messages("memory-manager")]
            return True

        monkeypatch.setattr(cohort, "_kill", kill)
        cohort.collect("memory-manager")
        assert seen[children[0]] == [], "report still pending when the child was killed"

    def test_deadline_kills_and_records_the_straggler(self, isolate):
        children = _fan_out(isolate, n=1, ttl=-1)
        data = cohort.collect("memory-manager")
        assert data["children"][0]["state"] == "timeout"
        assert isolate["killed"] == [children[0]]


class TestWorktreeChildrenAreNotTornDown:
    """#756 — a worktree child holds a branch and possibly an open PR, whose
    teardown follows merge verification, and its session is where a reviewer
    sends fix-ups. Cohort teardown must never kill it; an abandoned one is
    already surfaced by `worktree --dangling`. The `agentwire new` children of
    the 2026-08-01 leak are main topology and DO get torn down."""

    def _worktree_fan_out(self, state, parent="orchestrator"):
        state["live"].update({parent, "proj-feature"})
        cohort.enroll(parent, "proj-feature", topology=cohort.WORKTREE)
        return "proj-feature"

    def test_reported_worktree_child_is_left_running(self, isolate):
        child = self._worktree_fan_out(isolate)
        inbox.enqueue("orchestrator", "PR #42 drafted", kind="done", sender=child)
        result = cohort.wait("orchestrator", timeout=5)
        assert result["reports"][0]["report"] == "PR #42 drafted"
        assert result["left_alive"] == [child]
        assert isolate["killed"] == []


class TestWaitCli:
    def test_exit_status_reflects_resolution(self, isolate, capsys):
        from agentwire import wait_cli

        children = _fan_out(isolate, n=1)
        args = SimpleNamespace(session="memory-manager", timeout=0, json=False,
                               children=True)
        assert wait_cli.cmd_wait(args) == 1  # still pending → caller loops
        inbox.enqueue("memory-manager", "all done", kind="done", sender=children[0])
        assert wait_cli.cmd_wait(args) == 0
        assert "all done" in capsys.readouterr().out

