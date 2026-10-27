"""Tests for the agentwire.scheduler package — Format helpers, pick logic, board I/O."""

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agentwire.scheduler import (
    Board,
    Schedule,
    SchedulerTask,
    TaskState,
    pick_next_task,
)

# --- format_interval ---



# --- format_overdue ---



# --- _check_gate: precondition gates (git_commit, git_diff, command) ---

class TestCheckGate:
    """End-to-end tests against a real git repo fixture in tmp_path.

    Gates are AND'd: all must pass for _check_gate to return True. Failure
    in subprocess (git not found, malformed cmd) fails OPEN — gate returns
    True so the task can run.
    """

    @pytest.fixture(autouse=True)
    def _no_real_board_write(self):
        # A gate skip now persists `last_gate_skip` (#803) — patch save_board
        # so these pure decision-logic tests never touch the real
        # ~/.agentwire/scheduler-state.yaml (same hazard TestGateError already
        # guards against for the gate-error path). Also reset the module-level
        # `_gated_tasks` dedup set: it's keyed by task name ("t", shared by
        # every test in this class) and only the FIRST transition into a
        # gated state writes `last_gate_skip` — a leftover entry from an
        # earlier test would silently skip that write here too.
        import agentwire.scheduler as sched
        sched._gated_tasks.clear()
        with patch("agentwire.scheduler.save_board"):
            yield
        sched._gated_tasks.clear()

    @pytest.fixture
    def git_project(self, tmp_path):
        """Initialize a git repo with one commit; return the path."""
        import subprocess as sp
        sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        sp.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        sp.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("hello\n")
        sp.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        sp.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
        return tmp_path

    @pytest.fixture
    def board(self, git_project):
        from agentwire.scheduler import Board, SchedulerTask, TaskState
        task = SchedulerTask(name="t", project=str(git_project))
        return Board(tasks={"t": task}, state={"t": TaskState()})












    def test_command_nonzero_exit_blocks(self, board, git_project):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"command": "false"}
        assert _check_gate(board, "t") is False








# --- _check_gate: gate-eval errors surface instead of vanishing ---

class TestGateError:
    """A gate-eval exception (e.g. git timeout) must still fail OPEN, but no
    longer silently — it's logged to the event stream and recorded on state.
    """

    @pytest.fixture(autouse=True)
    def _reset_spam_state(self):
        import agentwire.scheduler as sched
        sched._gate_errored.clear()
        yield
        sched._gate_errored.clear()

    @pytest.fixture
    def board(self, tmp_path):
        from agentwire.scheduler import Board, SchedulerTask, TaskState
        task = SchedulerTask(name="t", project=str(tmp_path))
        task.gate = {"git_commit": True}
        board = Board(tasks={"t": task}, state={"t": TaskState()})
        board.state["t"].last_gate_commit = "deadbeef"  # force gate to evaluate
        return board

    def test_timeout_fails_open_and_records(self, board):
        import subprocess
        from unittest.mock import patch

        from agentwire.scheduler import _check_gate

        boom = subprocess.TimeoutExpired(cmd="git", timeout=5)
        with patch("agentwire.scheduler.subprocess.run", side_effect=boom), \
             patch("agentwire.scheduler.save_board") as save, \
             patch("agentwire.scheduler._log_event") as log:
            assert _check_gate(board, "t") is True  # still fails OPEN

        assert "git_commit" in board.state["t"].last_gate_error
        assert "TimeoutExpired" in board.state["t"].last_gate_error
        save.assert_called_once()  # persisted so the board can show it
        log.assert_called_once()
        assert log.call_args.args[0] == "gate_error"





# --- _EXIT_TO_STATUS mapping ---



# --- pick_next_task ---

class TestPickNextTask:
    def _make_board(self, tasks_and_states):
        """Helper: build a Board from list of (name, every, enabled, filler, last_run_ts)."""
        board = Board()
        for name, every, enabled, filler, last_run_ts in tasks_and_states:
            board.tasks[name] = SchedulerTask(
                name=name,
                project="/tmp/test",
                session=name,
                task=name,
                schedule=Schedule(every=every),
                enabled=enabled,
                filler=filler,
            )
            if last_run_ts > 0:
                dt = datetime.fromtimestamp(last_run_ts, tz=timezone.utc)
                board.state[name] = TaskState(last_run=dt, last_status="complete")
        return board

    @patch("agentwire.scheduler._check_gate", return_value=True)
    def test_most_overdue_wins(self, mock_gate):
        now = time.time()
        board = self._make_board([
            ("task-a", "1h", True, False, now - 7200),  # 1h overdue
            ("task-b", "1h", True, False, now - 10800), # 2h overdue
        ])
        name, wait = pick_next_task(board)
        assert name == "task-b"  # More overdue
        assert wait == 0.0




    @patch("agentwire.scheduler._check_gate", return_value=False)
    def test_due_but_gate_blocked_sleeps_not_spins(self, mock_gate):
        """#691: a due task whose gate fails must NOT yield (None, 0.0) —
        time.sleep(0) makes the daemon busy-loop (gate re-run + portal notify
        ~12×/sec) until the gate ever passes."""
        from agentwire.scheduler.schedule import GATE_RETRY_FLOOR

        now = time.time()
        board = self._make_board([
            ("gated-task", "1h", True, False, now - 7200),  # overdue, gate fails
        ])
        name, wait = pick_next_task(board)
        assert name is None
        assert wait >= GATE_RETRY_FLOOR



# --- Ensure-task validation ---







class TestReapWorktreePrs:
    """The reaper tears down worktrees only when their PR is merged/closed."""

    def _board_with_pr(self, state="OPEN"):
        from agentwire.scheduler import Board, Schedule, SchedulerTask, TaskState
        board = Board()
        board.tasks["t"] = SchedulerTask(name="t", project="/tmp/p", session="t",
                                         task="t", schedule=Schedule(every="1h"))
        board.state["t"] = TaskState(
            pr_number=42, worktree_path="/tmp/p-worktrees/scheduler-t-x",
            worktree_branch="scheduler-t-x", worktree_session="p/scheduler-t-x",
        )
        return board

    def _patch(self, monkeypatch, pr_state):
        from agentwire import scheduler
        killed, removed = [], []
        monkeypatch.setattr(scheduler, "_pr_state", lambda n, cwd: pr_state)
        monkeypatch.setattr(scheduler, "_kill_session", lambda s: killed.append(s))
        monkeypatch.setattr(scheduler, "_remove_scheduler_worktree",
                            lambda p, b, proj=None: removed.append((p, b)))
        monkeypatch.setattr(scheduler, "save_board", lambda b: None)
        return killed, removed

    def test_open_pr_not_reaped(self, monkeypatch):
        from agentwire import scheduler
        killed, removed = self._patch(monkeypatch, "OPEN")
        board = self._board_with_pr()
        assert scheduler.reap_worktree_prs(board) == []
        assert killed == [] and removed == []
        assert board.state["t"].pr_number == 42  # untouched

    def test_merged_pr_reaped(self, monkeypatch):
        from agentwire import scheduler
        killed, removed = self._patch(monkeypatch, "MERGED")
        board = self._board_with_pr()
        reaped = scheduler.reap_worktree_prs(board)
        assert len(reaped) == 1 and reaped[0]["pr_state"] == "MERGED"
        assert killed == ["p/scheduler-t-x"]
        assert removed == [("/tmp/p-worktrees/scheduler-t-x", "scheduler-t-x")]
        # Tracking fields cleared.
        st = board.state["t"]
        assert st.pr_number is None and st.worktree_path == "" and st.worktree_session == ""




class TestPersistentSessionDispatch:
    """Tasks with exit_on_complete: false must not have their session killed
    at dispatch time (issue #234) — the live session is reused by ensure."""

    def _project(self, tmp_path, task_yaml: str):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentwire.tasks.yml").write_text(task_yaml)
        return proj

    def _sched_task(self, proj, **kwargs):
        return SchedulerTask(name="t", project=str(proj), session="persist-s",
                             task="t", schedule=Schedule(every="1h"), **kwargs)

    # --- _task_is_persistent ---




    # --- _dispatch_inplace_task kill behavior ---

    def _patch_dispatch(self, monkeypatch):
        import agentwire.locking
        from agentwire import scheduler
        killed, precreated = [], []
        monkeypatch.setattr(agentwire.locking, "remove_stale_lock", lambda s: None)
        monkeypatch.setattr(scheduler, "_kill_session", lambda s: killed.append(s))
        monkeypatch.setattr(scheduler, "_pre_create_session", lambda t: precreated.append(t.session))
        monkeypatch.setattr(scheduler, "_run_ensure", lambda cmd, env=None: (0, None, 1))
        monkeypatch.setattr(scheduler, "_parse_ensure_summary", lambda t, r: ("ok", [], []))
        monkeypatch.setattr(scheduler, "_log_event", lambda *a, **k: None)
        monkeypatch.setattr(scheduler, "_notify_portal", lambda *a, **k: None)
        monkeypatch.setattr(scheduler, "_capture_head", lambda p: "")
        monkeypatch.setattr(scheduler, "save_board", lambda b: None)
        return killed, precreated

    def _dispatch(self, proj, **task_kwargs):
        from agentwire import scheduler
        task = self._sched_task(proj, **task_kwargs)
        board = Board()
        board.tasks["t"] = task
        return scheduler._dispatch_inplace_task(board, task, TaskState())

    def test_persistent_task_session_not_killed(self, tmp_path, monkeypatch):
        proj = self._project(tmp_path, "tasks:\n  t:\n    prompt: do\n    exit_on_complete: false\n")
        killed, _ = self._patch_dispatch(monkeypatch)
        state = self._dispatch(proj)
        assert killed == []
        assert state.last_status == "complete"





class TestDispatchWatchdog:
    """#677: a hung ensure must never starve the board. _run_ensure gets a
    max-runtime ceiling, self-heals from a dead child whose pipes are held
    open by grandchildren, and the timeout aftermath is loud (session kill +
    owner notification)."""

    def _fast_watchdog(self, monkeypatch, max_runtime):
        from types import SimpleNamespace

        from agentwire import scheduler
        from agentwire.scheduler import dispatch
        monkeypatch.setattr(dispatch, "_WATCHDOG_POLL", 0.1)
        monkeypatch.setattr(
            scheduler, "_sched_config",
            lambda: SimpleNamespace(dispatch_max_runtime=max_runtime))

    # --- _run_ensure ---


    def test_dead_child_with_held_pipes_reaped(self, monkeypatch):
        # Child exits immediately but a background grandchild inherits the
        # stdout pipe — bare communicate() would block on pipe EOF forever.
        self._fast_watchdog(monkeypatch, 3600)
        from agentwire import scheduler
        start = time.time()
        exit_code, result, duration = scheduler._run_ensure(
            ["sh", "-c", "echo hi; sleep 60 & exit 0"])
        assert exit_code == 0
        assert time.time() - start < 30  # reaped by the poll, not pipe EOF



    # --- dispatch aftermath ---

    def _patch_dispatch(self, monkeypatch, proj):
        import agentwire.locking
        from agentwire import scheduler
        from agentwire.scheduler import _EXIT_TIMEOUT
        killed, notified = [], []
        monkeypatch.setattr(agentwire.locking, "remove_stale_lock", lambda s: None)
        monkeypatch.setattr(scheduler, "_kill_session", lambda s: killed.append(s))
        monkeypatch.setattr(scheduler, "_pre_create_session", lambda t: None)
        monkeypatch.setattr(scheduler, "_run_ensure",
                            lambda cmd, env=None: (_EXIT_TIMEOUT, None, 999))
        monkeypatch.setattr(scheduler, "_parse_ensure_summary",
                            lambda t, r, **kw: ("", [], []))
        monkeypatch.setattr(scheduler, "_notify_dispatch_timeout",
                            lambda t, s, d: notified.append((t.name, s, d)))
        monkeypatch.setattr(scheduler, "_log_event", lambda *a, **k: None)
        monkeypatch.setattr(scheduler, "_notify_portal", lambda *a, **k: None)
        monkeypatch.setattr(scheduler, "_capture_head", lambda p: "")
        monkeypatch.setattr(scheduler, "save_board", lambda b: None)
        return killed, notified

    def _project(self, tmp_path, task_yaml):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentwire.tasks.yml").write_text(task_yaml)
        return proj

    def _dispatch(self, proj, **task_kwargs):
        from agentwire import scheduler
        task = SchedulerTask(name="t", project=str(proj), session="wd-s",
                             task="t", schedule=Schedule(every="1h"), **task_kwargs)
        board = Board()
        board.tasks["t"] = task
        return scheduler._dispatch_inplace_task(board, task, TaskState())


