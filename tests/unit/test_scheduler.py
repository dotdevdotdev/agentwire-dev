"""Tests for agentwire/scheduler.py — Format helpers, pick logic, board I/O."""

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agentwire.scheduler import (
    Board,
    Schedule,
    SchedulerTask,
    TaskState,
    format_interval,
    format_overdue,
    pick_next_task,
    _EXIT_TO_STATUS,
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
        from agentwire.scheduler import _check_gate
        import subprocess as sp
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


class TestAutoCommitScoping:
    """The scheduler must commit only what a task touched — never sweep up
    unrelated in-flight work (regression for the `git add -A` hazard)."""

    def _repo(self, tmp_path):
        import subprocess
        d = str(tmp_path)

        def git(*a):
            return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (tmp_path / "README.md").write_text("base\n")
        (tmp_path / ".agentwire.yml").write_text("type: claude-bypass\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        return d, git

    def _task(self, project):
        return SchedulerTask(name="t", project=project, session="t", task="t",
                             schedule=Schedule(every="1h"))

    def test_dirty_paths_reports_renames_destination(self, tmp_path):
        from agentwire.scheduler import _dirty_paths
        d, git = self._repo(tmp_path)
        git("mv", "README.md", "DOCS.md")
        assert "DOCS.md" in _dirty_paths(d)

    def test_pre_existing_work_is_not_committed(self, tmp_path):
        from agentwire.scheduler import _auto_commit, _dirty_paths
        d, git = self._repo(tmp_path)

        # Unrelated in-flight work present before the task runs.
        (tmp_path / "human_work.py").write_text("# precious\n")
        with open(tmp_path / "README.md", "a") as f:
            f.write("human edit\n")
        pre = _dirty_paths(d)

        # Task creates its own output.
        (tmp_path / "task_output.txt").write_text("result\n")
        _auto_commit(self._task(d), "briefing", "complete", pre)

        committed = git("show", "--name-only", "--format=", "HEAD").stdout.split()
        assert committed == ["task_output.txt"]
        # Human work survives, uncommitted.
        status = git("status", "--porcelain").stdout
        assert "human_work.py" in status and "README.md" in status

    def test_agentwire_yml_is_protected(self, tmp_path):
        from agentwire.scheduler import _auto_commit, _dirty_paths
        d, git = self._repo(tmp_path)
        pre = _dirty_paths(d)

        # Task tampers with protected config and writes a legit output.
        (tmp_path / ".agentwire.yml").write_text("type: HACKED\n")
        (tmp_path / "out.txt").write_text("ok\n")
        _auto_commit(self._task(d), "t", "complete", pre)

        committed = git("show", "--name-only", "--format=", "HEAD").stdout.split()
        assert ".agentwire.yml" not in committed
        assert (tmp_path / ".agentwire.yml").read_text() == "type: claude-bypass\n"

    def test_no_commit_when_task_changed_nothing(self, tmp_path):
        from agentwire.scheduler import _auto_commit, _dirty_paths
        d, git = self._repo(tmp_path)
        (tmp_path / "human_work.py").write_text("# precious\n")
        pre = _dirty_paths(d)

        before = git("rev-parse", "HEAD").stdout.strip()
        _auto_commit(self._task(d), "t", "complete", pre)  # task touched nothing
        after = git("rev-parse", "HEAD").stdout.strip()
        assert before == after
