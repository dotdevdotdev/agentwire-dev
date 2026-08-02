"""Fan-out cohort ledger (#852).

A parent that fans out children went idle while *waiting*; the idle handler
read idle as done, reaped the parent mid-fan-out, and the children leaked while
their report-backs dead-lettered into owner email. These cover the ledger the
three consumers share: the join primitive, the idle-handler guard, and the
watchdog sweeper.
"""

import json
import time
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


class TestEnrollment:
    def test_creates_ledger_with_pending_children(self, isolate):
        _fan_out(isolate, n=2)
        data = cohort.load("memory-manager")
        assert [c["session"] for c in data["children"]] == [
            "memrev-child0", "memrev-child1"]
        assert {c["state"] for c in data["children"]} == {"pending"}
        assert data["task"] == "memory-manager"
        assert data["deadline"] > time.time()

    def test_reenrolling_the_same_child_is_a_noop(self, isolate):
        _fan_out(isolate, n=1)
        assert cohort.enroll("memory-manager", "memrev-child0") is False
        assert len(cohort.load("memory-manager")["children"]) == 1

    def test_self_enrollment_refused(self, isolate):
        assert cohort.enroll("solo", "solo") is False
        assert cohort.load("solo") is None

    def test_ledger_shape_matches_the_hook_guard(self, isolate):
        # The idle-handler guard reads `.children[].state == "pending"` and
        # `.deadline` with jq. Renaming either silently disarms it.
        _fan_out(isolate, n=1)
        raw = json.loads(cohort.ledger_path("memory-manager").read_text())
        assert raw["children"][0]["state"] == "pending"
        assert isinstance(raw["deadline"], int)


class TestBlocking:
    def test_pending_children_block(self, isolate):
        _fan_out(isolate, n=1)
        assert cohort.blocking("memory-manager") is True

    def test_deadline_bounds_the_suppression(self, isolate):
        # A wedged child must not pin a task alive forever.
        _fan_out(isolate, n=1, ttl=1)
        assert cohort.blocking("memory-manager", now=int(time.time()) + 60) is False

    def test_resolved_cohort_stops_blocking(self, isolate):
        children = _fan_out(isolate, n=1)
        isolate["live"].discard(children[0])
        cohort.collect("memory-manager")
        assert cohort.blocking("memory-manager") is False

    def test_no_ledger_never_blocks(self, isolate):
        assert cohort.blocking("nobody") is False

    def test_corrupt_ledger_fails_open(self, isolate):
        path = cohort.ledger_path("memory-manager")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json")
        assert cohort.load("memory-manager") is None
        assert cohort.blocking("memory-manager") is False


class TestCollect:
    def _report(self, parent, child, text="done: 3 memories pruned"):
        inbox.enqueue(parent, text, kind="done", sender=child)

    def test_report_is_collected_and_child_killed(self, isolate):
        children = _fan_out(isolate, n=1)
        self._report("memory-manager", children[0])
        data = cohort.collect("memory-manager")
        entry = data["children"][0]
        assert entry["state"] == "reported"
        assert "3 memories pruned" in entry["report"]
        assert isolate["killed"] == [children[0]]

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

    def test_vanished_child_is_marked_gone_not_killed(self, isolate):
        children = _fan_out(isolate, n=1)
        isolate["live"].discard(children[0])
        data = cohort.collect("memory-manager")
        assert data["children"][0]["state"] == "gone"
        assert isolate["killed"] == []

    def test_silent_child_stays_pending_before_the_deadline(self, isolate):
        _fan_out(isolate, n=1)
        data = cohort.collect("memory-manager")
        assert data["children"][0]["state"] == "pending"
        assert isolate["killed"] == []

    def test_deadline_kills_and_records_the_straggler(self, isolate):
        children = _fan_out(isolate, n=1, ttl=-1)
        data = cohort.collect("memory-manager")
        assert data["children"][0]["state"] == "timeout"
        assert isolate["killed"] == [children[0]]

    def test_ingest_pointer_is_not_a_report(self, isolate):
        # Passive kinds are pull-only pointers the parent reads separately —
        # they must not resolve a child (or get eaten out of the inbox).
        children = _fan_out(isolate, n=1)
        inbox.enqueue("memory-manager", "see report", kind="ingest",
                      sender=children[0], ref="/tmp/report.md")
        data = cohort.collect("memory-manager")
        assert data["children"][0]["state"] == "pending"

    def test_foreign_sender_does_not_resolve_a_child(self, isolate):
        _fan_out(isolate, n=1)
        inbox.enqueue("memory-manager", "unrelated", kind="done", sender="someone-else")
        data = cohort.collect("memory-manager")
        assert data["children"][0]["state"] == "pending"
        assert [m.sender for m in inbox.list_messages("memory-manager")] == ["someone-else"]


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

    def test_silent_worktree_child_is_not_killed_at_the_deadline(self, isolate):
        isolate["live"].update({"orchestrator", "proj-feature"})
        cohort.enroll("orchestrator", "proj-feature", ttl=-1,
                      topology=cohort.WORKTREE)
        result = cohort.wait("orchestrator", timeout=5)
        assert result["failed"] == [{"session": "proj-feature", "state": "timeout"}]
        assert isolate["killed"] == []

    def test_dead_parent_does_not_reap_a_worktree_child(self, isolate):
        # The uncommitted-work case: killing here would trade a visible
        # dangling PR for a silently destroyed working tree.
        child = self._worktree_fan_out(isolate)
        isolate["live"].discard("orchestrator")
        result = cohort.sweep()
        assert result["reaped"] == []
        assert isolate["killed"] == []
        assert child in isolate["live"]
        assert cohort.load("orchestrator") is None  # ledger still cleaned up

    def test_main_topology_child_is_torn_down(self, isolate):
        children = _fan_out(isolate, n=1)
        inbox.enqueue("memory-manager", "done", kind="done", sender=children[0])
        result = cohort.wait("memory-manager", timeout=5)
        assert result["left_alive"] == []
        assert isolate["killed"] == children


class TestWait:
    def test_resolves_and_drops_the_ledger(self, isolate):
        children = _fan_out(isolate, n=2)
        for child in children:
            inbox.enqueue("memory-manager", f"{child} done", kind="done", sender=child)
        result = cohort.wait("memory-manager", timeout=5)
        assert result["resolved"] is True
        assert {r["session"] for r in result["reports"]} == set(children)
        assert sorted(isolate["killed"]) == sorted(children)
        # Ledger gone → the idle guard stops suppressing immediately.
        assert cohort.load("memory-manager") is None

    def test_returns_pending_when_this_call_times_out(self, isolate):
        # Re-callable: a fan-out longer than the harness's tool timeout loops.
        children = _fan_out(isolate, n=1)
        result = cohort.wait("memory-manager", timeout=0, poll=0)
        assert result["resolved"] is False
        assert result["pending"] == children
        assert cohort.load("memory-manager") is not None

    def test_no_cohort_returns_immediately(self, isolate):
        result = cohort.wait("lonely", timeout=99)
        assert result == {"parent": "lonely", "cohort": False, "resolved": True,
                          "pending": [], "reports": [], "failed": [],
                          "left_alive": [], "children": []}

    def test_straggler_surfaces_as_a_failure(self, isolate):
        children = _fan_out(isolate, n=1, ttl=-1)
        result = cohort.wait("memory-manager", timeout=5)
        assert result["resolved"] is True
        assert result["failed"] == [{"session": children[0], "state": "timeout"}]
        assert isolate["killed"] == children


class TestSweep:
    def test_dead_parent_reaps_its_children(self, isolate):
        # The crash path (usage limit, guard deadline, /exit) that leaks
        # children under every child-side mechanism: a wedged child never goes
        # idle, so it never self-kills.
        children = _fan_out(isolate, n=2)
        isolate["live"].discard("memory-manager")
        result = cohort.sweep()
        assert sorted(e["child"] for e in result["reaped"]) == sorted(children)
        assert sorted(isolate["killed"]) == sorted(children)
        assert cohort.load("memory-manager") is None

    def test_live_parent_keeps_its_ledger(self, isolate):
        _fan_out(isolate, n=1)
        cohort.sweep()
        assert cohort.load("memory-manager") is not None
        assert isolate["killed"] == []

    def test_live_parent_ledger_self_clears_as_children_exit(self, isolate):
        children = _fan_out(isolate, n=1)
        isolate["live"].discard(children[0])
        cohort.sweep()
        assert cohort.load("memory-manager")["children"][0]["state"] == "gone"
        assert cohort.blocking("memory-manager") is False

    def test_never_joined_cohort_is_reaped_after_the_grace(self, isolate):
        children = _fan_out(isolate, n=1,
                            ttl=-(cohort.STALE_GRACE + 60))
        cohort.sweep()
        assert isolate["killed"] == children
        assert cohort.load("memory-manager") is None

    def test_missing_root_is_a_noop(self, isolate):
        assert cohort.sweep() == {"reaped": [], "swept": []}

    def test_corrupt_ledger_is_skipped_not_fatal(self, isolate):
        path = cohort.ledger_path("broken")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("nope")
        _fan_out(isolate, n=1)
        isolate["live"].discard("memory-manager")
        assert cohort.sweep()["reaped"]  # the healthy cohort still got swept


class TestDrainHold:
    """#852 — a report from a still-pending cohort child belongs to
    `wait --children`, which reads it off disk and consumes it before tearing
    the child down. Letting the drain paste it first races that collection
    (leaving the child unresolved until its deadline) and pushes a long report
    through the delivery path #851 shows is fragile."""

    def test_pending_childs_report_is_held(self, isolate):
        children = _fan_out(isolate, n=1)
        inbox.enqueue("memory-manager", "my report", kind="done", sender=children[0])
        result = inbox.flush_session("memory-manager")
        assert result["reason"] == "cohort_held"
        assert result["delivered"] == 0
        # Held WITHOUT penalty — a legitimate wait must not burn the report
        # toward dead-letter.
        assert [m.attempts for m in inbox.list_messages("memory-manager")] == [0]

    def test_other_senders_still_drain(self, isolate):
        _fan_out(isolate, n=1)
        inbox.enqueue("memory-manager", "unrelated", kind="note", sender="someone-else")
        assert inbox.flush_session("memory-manager")["reason"] != "cohort_held"

    def test_expired_cohort_releases_the_hold(self, isolate):
        # Past the deadline the join has given up, so the drain takes over
        # immediately rather than waiting for the sweeper's next pass.
        children = _fan_out(isolate, n=1, ttl=-1)
        inbox.enqueue("memory-manager", "late report", kind="done", sender=children[0])
        assert inbox.flush_session("memory-manager")["reason"] != "cohort_held"

    def test_resolved_cohort_stops_holding(self, isolate):
        children = _fan_out(isolate, n=1)
        cohort.discard("memory-manager")
        inbox.enqueue("memory-manager", "late report", kind="done", sender=children[0])
        assert inbox.flush_session("memory-manager")["reason"] != "cohort_held"


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
