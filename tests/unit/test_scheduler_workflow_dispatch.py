"""Integration tests for workflow-backed scheduler tasks.

Monkeypatches `run_workflow` and `resolve_workflow` so the dispatch path
exercises the real scheduler code without calling pi or requiring Z.AI credentials.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentwire.scheduler import (
    Board,
    Schedule,
    SchedulerTask,
    TaskState,
    dispatch_task,
)


@pytest.fixture
def workflow_env(tmp_path):
    """Scheduler config pointing at tmp paths + a fake workflow resolver."""

    class FakeSchedulerConfig:
        board_file = tmp_path / "scheduler.yaml"
        events_file = tmp_path / "events.jsonl"
        live_state_file = tmp_path / "live.json"
        git_timeout = 10
        git_op_timeout = 15
        gate_timeout = 10
        portal_notify_timeout = 5
        session_create_timeout = 30
        max_loop_sleep = 60
        dispatch_cooldown = 60

    # Empty board YAML so save_board() writes the state section without errors.
    FakeSchedulerConfig.board_file.write_text("tasks: {}\n")

    with patch("agentwire.scheduler._sched_config", return_value=FakeSchedulerConfig()), \
         patch("agentwire.scheduler._notify_portal", return_value=None), \
         patch("agentwire.scheduler._auto_commit", return_value=None), \
         patch("agentwire.scheduler._capture_head", return_value=""):
        yield FakeSchedulerConfig


def _fake_workflow(name: str = "demo"):
    wf = SimpleNamespace(name=name)
    wf.validate = lambda: []
    return wf


def _node(node_id, status="success", final_text="", error=None):
    return SimpleNamespace(
        node_id=node_id,
        status=status,
        final_text=final_text,
        error=error,
        duration_ms=100,
        attempts=1,
    )


def _make_board(**task_kwargs) -> Board:
    defaults = dict(
        name="demo-task",
        project="",
        session="",
        task="",
        workflow="demo",
        inputs={"topic": "{{ task }}"},
        schedule=Schedule(every="1h"),
    )
    defaults.update(task_kwargs)
    board = Board()
    board.tasks["demo-task"] = SchedulerTask(**defaults)
    board.state["demo-task"] = TaskState()
    return board


def _read_events(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestWorkflowDispatch:
    def test_success_maps_to_complete(self, workflow_env):
        board = _make_board()
        run = SimpleNamespace(
            workflow="demo",
            run_id="demo-20260416T100000-abcdef12",
            status="success",
            error=None,
            node_results=[
                _node("plan", "success", "a plan"),
                _node("act", "success", "did the thing"),
            ],
        )

        with patch("agentwire.workflows.definitions.resolve_workflow", return_value=_fake_workflow()), \
             patch("agentwire.workflows.runner.run_workflow", return_value=run):
            state = dispatch_task(board, "demo-task")

        assert state.last_status == "complete"
        assert state.run_count == 1

        events = _read_events(workflow_env.events_file)
        completed = [e for e in events if e["event"] == "task_completed"]
        assert len(completed) == 1
        ev = completed[0]
        assert ev["status"] == "complete"
        assert ev["workflow"] == "demo"
        assert ev["run_id"] == run.run_id
        assert [n["id"] for n in ev["nodes"]] == ["plan", "act"]

    def test_partial_maps_to_incomplete(self, workflow_env):
        board = _make_board()
        run = SimpleNamespace(
            workflow="demo",
            run_id="demo-run-1",
            status="partial",
            error=None,
            node_results=[
                _node("a", "success", "ok"),
                _node("b", "skipped", ""),
            ],
        )

        with patch("agentwire.workflows.definitions.resolve_workflow", return_value=_fake_workflow()), \
             patch("agentwire.workflows.runner.run_workflow", return_value=run):
            state = dispatch_task(board, "demo-task")

        assert state.last_status == "incomplete"

    def test_failure_maps_to_failed_with_blockers(self, workflow_env):
        board = _make_board()
        run = SimpleNamespace(
            workflow="demo",
            run_id="demo-run-2",
            status="failure",
            error="node[a] failed after 1 attempt(s): boom",
            node_results=[
                _node("a", "failure", "", error="boom"),
            ],
        )

        with patch("agentwire.workflows.definitions.resolve_workflow", return_value=_fake_workflow()), \
             patch("agentwire.workflows.runner.run_workflow", return_value=run):
            state = dispatch_task(board, "demo-task")

        assert state.last_status == "failed"
        events = _read_events(workflow_env.events_file)
        ev = [e for e in events if e["event"] == "task_completed"][0]
        assert any("node[a] failed" in b for b in ev["blockers"])
        assert any("[a] boom" in b for b in ev["blockers"])

    def test_resolve_workflow_failure_records_failed_state(self, workflow_env):
        board = _make_board(workflow="__does_not_exist__xyz__")

        with patch(
            "agentwire.workflows.definitions.resolve_workflow",
            side_effect=FileNotFoundError("workflow not found"),
        ):
            state = dispatch_task(board, "demo-task")

        assert state.last_status == "failed"
        events = _read_events(workflow_env.events_file)
        ev = [e for e in events if e["event"] == "task_completed"][0]
        assert "not found" in " ".join(ev.get("blockers", []))

    def test_inputs_rendered_before_run(self, workflow_env):
        board = _make_board(inputs={"topic": "{{ task }}", "where": "{{ project }}"})
        board.tasks["demo-task"].project = "/tmp/foo"

        captured = {}

        def fake_run(wf, runs_dir=None, inputs=None, dry_run=False):
            captured["inputs"] = inputs
            return SimpleNamespace(
                workflow=wf.name,
                run_id="x",
                status="success",
                error=None,
                node_results=[_node("a", "success", "done")],
            )

        with patch("agentwire.workflows.definitions.resolve_workflow", return_value=_fake_workflow()), \
             patch("agentwire.workflows.runner.run_workflow", side_effect=fake_run):
            dispatch_task(board, "demo-task")

        assert captured["inputs"] == {"topic": "demo-task", "where": "/tmp/foo"}

    def test_max_runs_disables_after_final_run(self, workflow_env):
        board = _make_board(max_runs=1)
        run = SimpleNamespace(
            workflow="demo",
            run_id="demo-run-3",
            status="success",
            error=None,
            node_results=[_node("a", "success", "ok")],
        )

        with patch("agentwire.workflows.definitions.resolve_workflow", return_value=_fake_workflow()), \
             patch("agentwire.workflows.runner.run_workflow", return_value=run):
            dispatch_task(board, "demo-task")

        assert board.tasks["demo-task"].enabled is False
        events = _read_events(workflow_env.events_file)
        assert any(e["event"] == "task_disabled" for e in events)
