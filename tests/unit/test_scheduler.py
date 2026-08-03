"""Tests for the agentwire.scheduler package — Format helpers, pick logic, board I/O."""

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agentwire.scheduler import (
    _EXIT_TO_STATUS,
    Board,
    Schedule,
    SchedulerTask,
    TaskState,
    format_interval,
    format_overdue,
    pick_next_task,
)

# --- format_interval ---

class TestFormatInterval:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0s"),
        (30, "30s"),
        (45, "45s"),
        (59, "59s"),
        (60, "1m"),
        (120, "2m"),
        (3600, "1h"),
        (3660, "1h1m"),
        (7200, "2h"),
        (86400, "1d"),
        (90000, "1d1h"),
        (172800, "2d"),
    ])
    def test_formatting(self, seconds, expected):
        assert format_interval(seconds) == expected


# --- format_overdue ---

class TestFormatOverdue:
    @pytest.mark.parametrize("seconds,expected", [
        (3600.0, "+1h"),
        (-1800.0, "-30m"),
        (0.0, "+0s"),
        (45.0, "+45s"),
    ])
    def test_format_overdue(self, seconds, expected):
        assert format_overdue(seconds) == expected


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

    def _head(self, project):
        import subprocess as sp
        return sp.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def _new_commit(self, project, file="other.txt", content="x"):
        import subprocess as sp
        (project / file).write_text(content)
        sp.run(["git", "-C", str(project), "add", "-A"], check=True)
        sp.run(["git", "-C", str(project), "commit", "-qm", "next"], check=True)

    def test_no_gate_passes(self, board):
        from agentwire.scheduler import _check_gate
        assert _check_gate(board, "t") is True

    def test_git_commit_no_baseline_passes(self, board):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"git_commit": True}
        assert _check_gate(board, "t") is True

    def test_git_commit_unchanged_blocks(self, board, git_project):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"git_commit": True}
        board.state["t"].last_gate_commit = self._head(git_project)
        assert _check_gate(board, "t") is False

    def test_git_commit_advanced_passes(self, board, git_project):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"git_commit": True}
        old_head = self._head(git_project)
        board.state["t"].last_gate_commit = old_head
        self._new_commit(git_project)
        assert _check_gate(board, "t") is True

    def test_git_commit_invalid_project_fails_open(self, board, tmp_path):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"git_commit": True}
        board.tasks["t"].project = str(tmp_path / "not-a-repo")
        board.state["t"].last_gate_commit = "deadbeef"
        assert _check_gate(board, "t") is True  # fail open

    def test_git_diff_no_changes_in_paths_blocks(self, board, git_project):
        from agentwire.scheduler import _check_gate
        old_head = self._head(git_project)
        self._new_commit(git_project, file="other.txt")
        board.tasks["t"].gate = {"git_diff": ["src/"]}
        board.state["t"].last_gate_commit = old_head
        assert _check_gate(board, "t") is False

    def test_git_diff_changes_in_paths_passes(self, board, git_project):
        import subprocess as sp

        from agentwire.scheduler import _check_gate
        old_head = self._head(git_project)
        (git_project / "watched.txt").write_text("changed")
        sp.run(["git", "-C", str(git_project), "add", "-A"], check=True)
        sp.run(["git", "-C", str(git_project), "commit", "-qm", "watched"], check=True)
        board.tasks["t"].gate = {"git_diff": ["watched.txt"]}
        board.state["t"].last_gate_commit = old_head
        assert _check_gate(board, "t") is True

    def test_git_diff_no_baseline_passes(self, board):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"git_diff": ["src/"]}
        assert _check_gate(board, "t") is True

    def test_command_zero_exit_passes(self, board, git_project):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"command": "true"}
        assert _check_gate(board, "t") is True

    def test_command_nonzero_exit_blocks(self, board, git_project):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"command": "false"}
        assert _check_gate(board, "t") is False

    def test_command_with_pipe_runs_via_shell(self, board, git_project):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"command": "echo ok | grep -q ok"}
        assert _check_gate(board, "t") is True

    def test_multiple_gates_all_required(self, board, git_project):
        from agentwire.scheduler import _check_gate
        old = self._head(git_project)
        board.state["t"].last_gate_commit = old
        self._new_commit(git_project)
        board.tasks["t"].gate = {"git_commit": True, "command": "false"}
        assert _check_gate(board, "t") is False

    def test_command_gate_skip_records_last_gate_skip(self, board, git_project):
        # A clean "not ready yet" skip (distinct from a gate-eval exception)
        # is recorded on state so the board can show it instead of reading
        # as silently-falling-behind overdue (#803).
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"command": "false"}
        assert _check_gate(board, "t") is False
        assert "command" in board.state["t"].last_gate_skip
        assert "exit 1" in board.state["t"].last_gate_skip

    def test_gate_pass_clears_last_gate_skip(self, board, git_project):
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"command": "false"}
        _check_gate(board, "t")
        assert board.state["t"].last_gate_skip

        board.tasks["t"].gate = {"command": "true"}
        assert _check_gate(board, "t") is True
        assert board.state["t"].last_gate_skip == ""

    def test_no_gate_clears_stale_last_gate_skip(self, board, git_project):
        # Removing the gate entirely (not just satisfying it) must also
        # clear a stale skip note — same "gate no longer blocking" outcome.
        from agentwire.scheduler import _check_gate
        board.tasks["t"].gate = {"command": "false"}
        _check_gate(board, "t")
        assert board.state["t"].last_gate_skip

        board.tasks["t"].gate = None
        assert _check_gate(board, "t") is True
        assert board.state["t"].last_gate_skip == ""

    def test_gate_skip_reason_updates_when_blocker_changes(self, board, git_project):
        # Multi-condition gate: the blocker shifts from git_commit to
        # command WITHOUT the gate ever fully passing in between, so
        # `_gated_tasks` (the log-spam dedup) never clears. The board must
        # still track the CURRENT blocker, not the original one.
        from agentwire.scheduler import _check_gate
        old = self._head(git_project)
        board.state["t"].last_gate_commit = old
        board.tasks["t"].gate = {"git_commit": True, "command": "false"}

        assert _check_gate(board, "t") is False
        assert "git_commit" in board.state["t"].last_gate_skip

        self._new_commit(git_project)  # git_commit now passes; command still fails
        assert _check_gate(board, "t") is False
        assert "command" in board.state["t"].last_gate_skip
        assert "git_commit" not in board.state["t"].last_gate_skip


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

    def test_error_logged_once_until_changes(self, board):
        import subprocess
        from unittest.mock import patch

        from agentwire.scheduler import _check_gate

        boom = subprocess.TimeoutExpired(cmd="git", timeout=5)
        with patch("agentwire.scheduler.subprocess.run", side_effect=boom), \
             patch("agentwire.scheduler.save_board"), \
             patch("agentwire.scheduler._log_event") as log:
            assert _check_gate(board, "t") is True
            assert _check_gate(board, "t") is True  # same error, no re-log
        assert log.call_count == 1

    def test_clears_when_gate_recovers(self, board, tmp_path):
        import subprocess
        from unittest.mock import patch

        from agentwire.scheduler import _check_gate

        boom = subprocess.TimeoutExpired(cmd="git", timeout=5)
        with patch("agentwire.scheduler.subprocess.run", side_effect=boom), \
             patch("agentwire.scheduler.save_board"), \
             patch("agentwire.scheduler._log_event"):
            _check_gate(board, "t")
        assert board.state["t"].last_gate_error

        # Gate now has no preconditions → clean pass clears the recorded error.
        board.tasks["t"].gate = {}
        with patch("agentwire.scheduler.save_board"):
            assert _check_gate(board, "t") is True
        assert board.state["t"].last_gate_error == ""

    def test_error_clears_stale_last_gate_skip(self, board):
        # A gate that was previously a clean skip (#803) and now errors
        # fails open (task runs) — a leftover "waiting on gate" note would
        # be actively misleading once the task is about to dispatch.
        import subprocess
        from unittest.mock import patch

        from agentwire.scheduler import _check_gate

        board.state["t"].last_gate_skip = "command: exit 1"
        boom = subprocess.TimeoutExpired(cmd="git", timeout=5)
        with patch("agentwire.scheduler.subprocess.run", side_effect=boom), \
             patch("agentwire.scheduler.save_board"), \
             patch("agentwire.scheduler._log_event"):
            assert _check_gate(board, "t") is True
        assert board.state["t"].last_gate_skip == ""


# --- _EXIT_TO_STATUS mapping ---

class TestExitCodeMapping:
    @pytest.mark.parametrize("code,status", [
        (0, "complete"),
        (1, "failed"),
        (2, "incomplete"),
        (3, "lock_conflict"),
        (4, "failed"),      # pre-failure mapped to failed
        (5, "timeout"),
        (6, "failed"),      # session-error mapped to failed
    ])
    def test_exit_to_status(self, code, status):
        assert _EXIT_TO_STATUS[code] == status


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

    @patch("agentwire.scheduler._check_gate", return_value=True)
    def test_disabled_skipped(self, mock_gate):
        now = time.time()
        board = self._make_board([
            ("enabled-task", "1m", True, False, now - 120),
            ("disabled-task", "1m", False, False, now - 120),
        ])
        name, wait = pick_next_task(board)
        assert name == "enabled-task"

    @patch("agentwire.scheduler._check_gate", return_value=True)
    def test_fillers_after_main(self, mock_gate):
        now = time.time()
        board = self._make_board([
            ("main-task", "1h", True, False, now - 60),  # Not overdue (1h interval, 60s ago)
            ("filler-task", "1m", True, True, now - 120),   # Overdue filler
        ])
        name, wait = pick_next_task(board)
        assert name == "filler-task"

    @patch("agentwire.scheduler._check_gate", return_value=True)
    def test_nothing_due_returns_wait(self, mock_gate):
        now = time.time()
        board = self._make_board([
            ("task-a", "1h", True, False, now - 10),  # 3590s until due
        ])
        name, wait = pick_next_task(board)
        assert name is None
        assert wait > 0

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

    @patch("agentwire.scheduler._check_gate", return_value=True)
    def test_never_run_task_is_overdue(self, mock_gate):
        board = self._make_board([
            ("new-task", "1h", True, False, 0),  # Never run (ts=0)
        ])
        name, wait = pick_next_task(board)
        assert name == "new-task"
        assert wait == 0.0


# --- Ensure-task validation ---

class TestValidateTaskPayload:
    def _task(self, **kwargs) -> SchedulerTask:
        defaults = dict(
            name="t",
            project="/tmp/p",
            session="t",
            task="t",
            schedule=Schedule(every="1h"),
        )
        defaults.update(kwargs)
        return SchedulerTask(**defaults)

    def test_ensure_task_passes(self):
        from agentwire.scheduler import _validate_task_payload
        errors = _validate_task_payload("t", self._task())
        assert errors == []

    def test_missing_task_rejected(self):
        from agentwire.scheduler import _validate_task_payload
        errors = _validate_task_payload("t", self._task(task=""))
        assert any("must set 'task'" in e for e in errors)

    def test_git_gate_requires_project(self):
        from agentwire.scheduler import _validate_task_payload
        errors = _validate_task_payload("t", self._task(project="", gate={"git_commit": True}))
        assert any("gate git_commit requires 'project' path" in e for e in errors)


class TestWorktreeMode:
    """Worktree+PR task-mode selection and PR-number parsing."""

    def _task(self, **kwargs):
        defaults = dict(name="t", project="/tmp/p", session="t", task="t",
                        schedule=Schedule(every="1h"))
        defaults.update(kwargs)
        return SchedulerTask(**defaults)

    def test_explicit_worktree_true_wins(self):
        from agentwire.scheduler import _is_worktree_task
        assert _is_worktree_task(self._task(worktree=True)) is True

    def test_explicit_worktree_false_wins(self, tmp_path):
        # Even a real git repo is skipped when explicitly disabled.
        import subprocess

        from agentwire.scheduler import _is_worktree_task
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"])
        t = self._task(project=str(tmp_path), worktree=False)
        assert _is_worktree_task(t) is False

    def test_auto_on_for_git_repo(self, tmp_path):
        import subprocess

        from agentwire.scheduler import _is_worktree_task
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"])
        assert _is_worktree_task(self._task(project=str(tmp_path))) is True

    def test_auto_off_for_non_repo(self, tmp_path):
        from agentwire.scheduler import _is_worktree_task
        assert _is_worktree_task(self._task(project=str(tmp_path))) is False

    def test_pr_number_from_url(self):
        from agentwire.scheduler import _pr_number_from_url
        assert _pr_number_from_url("https://github.com/o/r/pull/231\n") == 231
        assert _pr_number_from_url("no number here") is None


class TestFinalizeWorktree:
    """`_finalize_worktree_pr` no-change path (no network)."""

    def test_no_changes_removes_worktree_and_skips_pr(self, tmp_path, monkeypatch):
        import subprocess

        from agentwire import scheduler

        # A clean "worktree" (no uncommitted changes).
        d = tmp_path / "wt"
        d.mkdir()
        subprocess.run(["git", "-C", str(d), "init", "-q"])

        removed = {}
        monkeypatch.setattr(scheduler, "_remove_scheduler_worktree",
                            lambda p, b, proj=None: removed.update(path=p, branch=b))

        task = SchedulerTask(name="t", project=str(tmp_path), session="t", task="t",
                             schedule=Schedule(every="1h"))
        result = scheduler._finalize_worktree_pr(task, "t", "complete", str(d), "br", "")
        assert result == {}
        assert removed == {"path": str(d), "branch": "br"}


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

    def test_closed_pr_reaped(self, monkeypatch):
        from agentwire import scheduler
        self._patch(monkeypatch, "CLOSED")
        board = self._board_with_pr()
        assert len(scheduler.reap_worktree_prs(board)) == 1

    def test_pr_state_error_skips(self, monkeypatch):
        from agentwire import scheduler
        killed, removed = self._patch(monkeypatch, None)
        board = self._board_with_pr()
        assert scheduler.reap_worktree_prs(board) == []
        assert killed == [] and removed == []


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

    def test_persistent_when_exit_on_complete_false(self, tmp_path):
        from agentwire import scheduler
        proj = self._project(tmp_path, "tasks:\n  t:\n    prompt: do\n    exit_on_complete: false\n")
        assert scheduler._task_is_persistent(self._sched_task(proj)) is True

    def test_not_persistent_by_default(self, tmp_path):
        from agentwire import scheduler
        proj = self._project(tmp_path, "tasks:\n  t:\n    prompt: do\n")
        assert scheduler._task_is_persistent(self._sched_task(proj)) is False

    def test_not_persistent_when_task_unloadable(self, tmp_path):
        from agentwire import scheduler
        task = self._sched_task(tmp_path / "missing")
        assert scheduler._task_is_persistent(task) is False

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

    def test_default_task_session_killed(self, tmp_path, monkeypatch):
        proj = self._project(tmp_path, "tasks:\n  t:\n    prompt: do\n")
        killed, _ = self._patch_dispatch(monkeypatch)
        self._dispatch(proj)
        assert killed == ["persist-s"]

    def test_persistent_with_overrides_not_killed_but_precreated(self, tmp_path, monkeypatch):
        proj = self._project(tmp_path, "tasks:\n  t:\n    prompt: do\n    exit_on_complete: false\n")
        killed, precreated = self._patch_dispatch(monkeypatch)
        self._dispatch(proj, posture="bypass")
        assert killed == []
        assert precreated == ["persist-s"]  # no-op when session exists

    def test_overrides_without_persistence_killed_and_precreated(self, tmp_path, monkeypatch):
        proj = self._project(tmp_path, "tasks:\n  t:\n    prompt: do\n")
        killed, precreated = self._patch_dispatch(monkeypatch)
        self._dispatch(proj, posture="bypass")
        assert killed == ["persist-s"]
        assert precreated == ["persist-s"]


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

    def test_hung_child_killed_at_ceiling(self, monkeypatch):
        from agentwire import scheduler
        from agentwire.scheduler import _EXIT_TIMEOUT
        self._fast_watchdog(monkeypatch, 1)
        start = time.time()
        exit_code, result, duration = scheduler._run_ensure(["sleep", "60"])
        assert exit_code == _EXIT_TIMEOUT
        assert time.time() - start < 15  # nowhere near 60s
        assert result is not None and result.returncode == _EXIT_TIMEOUT

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

    def test_normal_completion_unaffected(self, monkeypatch):
        from agentwire import scheduler
        self._fast_watchdog(monkeypatch, 3600)
        exit_code, result, duration = scheduler._run_ensure(
            ["sh", "-c", "echo done"])
        assert exit_code == 0
        assert "done" in result.stdout

    def test_zero_ceiling_disables_watchdog_kill(self, monkeypatch):
        from agentwire import scheduler
        self._fast_watchdog(monkeypatch, 0)
        exit_code, result, duration = scheduler._run_ensure(
            ["sh", "-c", "sleep 0.3; echo ok"])
        assert exit_code == 0

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

    def test_timeout_kills_session_and_notifies(self, tmp_path, monkeypatch):
        proj = self._project(tmp_path, "tasks:\n  t:\n    prompt: do\n")
        killed, notified = self._patch_dispatch(monkeypatch, proj)
        state = self._dispatch(proj)
        assert state.last_status == "timeout"
        # killed once pre-dispatch (fresh session) and once post-timeout
        assert killed.count("wd-s") == 2
        assert notified == [("t", "wd-s", 999)]

    def test_timeout_spares_persistent_session_but_still_notifies(self, tmp_path, monkeypatch):
        proj = self._project(tmp_path,
                             "tasks:\n  t:\n    prompt: do\n    exit_on_complete: false\n")
        killed, notified = self._patch_dispatch(monkeypatch, proj)
        state = self._dispatch(proj)
        assert state.last_status == "timeout"
        assert killed == []
        assert notified == [("t", "wd-s", 999)]
