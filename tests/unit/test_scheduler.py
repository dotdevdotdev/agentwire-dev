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


# --- Workflow task validation ---

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

    def test_both_task_and_workflow_rejected(self):
        from agentwire.scheduler import _validate_task_payload
        errors = _validate_task_payload("t", self._task(task="t", workflow="demo"))
        # Workflow resolution will fail first because 'demo' doesn't exist; mutex check also fires.
        assert any("cannot set both" in e for e in errors)

    def test_neither_task_nor_workflow_rejected(self):
        from agentwire.scheduler import _validate_task_payload
        errors = _validate_task_payload("t", self._task(task="", workflow=""))
        assert any("must set either" in e for e in errors)

    def test_inputs_without_workflow_rejected(self):
        from agentwire.scheduler import _validate_task_payload
        errors = _validate_task_payload("t", self._task(inputs={"k": "v"}))
        assert any("'inputs' only valid with 'workflow'" in e for e in errors)

    def test_git_gate_requires_project(self):
        from agentwire.scheduler import _validate_task_payload
        errors = _validate_task_payload("t", self._task(project="", gate={"git_commit": True}))
        assert any("gate git_commit requires 'project' path" in e for e in errors)

    def test_unknown_workflow_rejected(self):
        from agentwire.scheduler import _validate_task_payload
        errors = _validate_task_payload(
            "t",
            self._task(task="", workflow="__does_not_exist__xyz__"),
        )
        assert any("not found" in e for e in errors)

    @pytest.mark.parametrize("session_type", [
        "sdk-bypass", "sdk-prompted", "sdk-restricted",
    ])
    def test_sdk_session_types_validate(self, session_type):
        # Phase 5: scheduled tasks accept the new SDK REPL session types.
        from agentwire.scheduler import _validate_task_payload
        errors = _validate_task_payload(
            "t",
            self._task(type=session_type),
        )
        assert errors == [], f"sdk type {session_type} should pass: {errors}"


class TestWorkflowStatusMapping:
    @pytest.mark.parametrize("wf_status,sched_status", [
        ("success", "complete"),
        ("partial", "incomplete"),
        ("failure", "failed"),
    ])
    def test_mapping(self, wf_status, sched_status):
        from agentwire.scheduler import _WORKFLOW_STATUS_TO_SCHED
        assert _WORKFLOW_STATUS_TO_SCHED[wf_status] == sched_status


class TestRenderWorkflowInputs:
    def _task(self, **kwargs) -> SchedulerTask:
        defaults = dict(
            name="my-task",
            project="/tmp/foo",
            session="sess",
            task="",
            workflow="demo",
            schedule=Schedule(every="1h"),
        )
        defaults.update(kwargs)
        return SchedulerTask(**defaults)

    def test_substitutes_known_vars(self):
        from agentwire.scheduler import _render_workflow_inputs
        task = self._task()
        out = _render_workflow_inputs(
            {"p": "{{ project }}", "t": "{{ task }}", "s": "{{ session }}", "w": "{{ workflow }}"},
            task,
        )
        assert out == {"p": "/tmp/foo", "t": "my-task", "s": "sess", "w": "demo"}

    def test_leaves_unknown_vars_untouched(self):
        from agentwire.scheduler import _render_workflow_inputs
        out = _render_workflow_inputs({"x": "{{ something_else }}"}, self._task())
        assert out == {"x": "{{ something_else }}"}

    def test_non_string_values_passthrough(self):
        from agentwire.scheduler import _render_workflow_inputs
        out = _render_workflow_inputs({"n": 5, "b": True, "lst": ["a"]}, self._task())
        assert out == {"n": 5, "b": True, "lst": ["a"]}

    def test_empty_inputs_returns_empty(self):
        from agentwire.scheduler import _render_workflow_inputs
        assert _render_workflow_inputs({}, self._task()) == {}
        assert _render_workflow_inputs(None, self._task()) == {}


class TestParseWorkflowSummary:
    def _run(self, status="success", node_results=None, error=None, workflow="demo"):
        from types import SimpleNamespace
        return SimpleNamespace(
            status=status,
            workflow=workflow,
            error=error,
            node_results=node_results or [],
        )

    def _node(self, node_id, status="success", final_text="", error=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            node_id=node_id, status=status, final_text=final_text, error=error,
            duration_ms=0, attempts=1,
        )

    def test_success_run(self):
        from agentwire.scheduler import _parse_workflow_summary
        run = self._run(
            status="success",
            node_results=[
                self._node("a", "success", "step one done"),
                self._node("b", "success", "final answer 42\nmore detail"),
            ],
        )
        summary, files, blockers = _parse_workflow_summary(run)
        assert "demo → success" in summary
        assert "a=success" in summary and "b=success" in summary
        assert "final answer 42" in summary
        assert files == []
        assert blockers == []

    def test_failure_run_collects_blockers(self):
        from agentwire.scheduler import _parse_workflow_summary
        run = self._run(
            status="failure",
            error="node[b] failed",
            node_results=[
                self._node("a", "success", ""),
                self._node("b", "failure", "", error="pi exited 2"),
            ],
        )
        summary, files, blockers = _parse_workflow_summary(run)
        assert summary.startswith("demo → failure")
        assert "node[b] failed" in blockers
        assert any("[b] pi exited 2" in b for b in blockers)

    def test_empty_nodes(self):
        from agentwire.scheduler import _parse_workflow_summary
        run = self._run(status="success", node_results=[])
        summary, _, _ = _parse_workflow_summary(run)
        assert "(no nodes)" in summary
