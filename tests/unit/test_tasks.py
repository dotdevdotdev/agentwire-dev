"""Tests for agentwire/tasks.py — Task parsing, validation, loading."""


import pytest

from agentwire.tasks import (
    TaskNotFound,
    load_task,
    parse_task_config,
)

# --- parse_pre_command ---



# --- parse_task_config ---

class TestParseTaskConfig:

    def test_full_config(self):
        config = {
            "prompt": "Do the thing.",
            "shell": "/bin/bash",
            "retries": 3,
            "retry_delay": 60,
            "idle_timeout": 45,
            "exit_on_complete": False,
            "mode": "loop",
            "max_iterations": 5,
            "pre": {
                "data": "echo hello",
            },
            "on_task_end": "Save results.",
            "post": ["echo done"],
            "output": {"capture": 100, "save": "~/logs/full.log"},
        }
        task = parse_task_config("full", config)
        assert task.shell == "/bin/bash"
        assert task.retries == 3
        assert task.retry_delay == 60
        assert task.idle_timeout == 45
        assert task.exit_on_complete is False
        assert task.mode == "loop"
        assert task.max_iterations == 5
        assert len(task.pre) == 1
        assert task.pre[0].name == "data"
        assert task.on_task_end == "Save results."
        assert task.post == ["echo done"]
        assert task.output.capture == 100
        assert task.output.save == "~/logs/full.log"






# --- validate_task ---



# --- load_task ---

class TestLoadTask:



    def test_does_not_read_agentwire_yml(self, project_dir):
        """Tasks live in .agentwire.tasks.yml only — .agentwire.yml is ignored (#720)."""
        (project_dir / ".agentwire.yml").write_text(
            "posture: bypass\ntasks:\n  lint:\n    prompt: Run lint.\n"
        )
        with pytest.raises(TaskNotFound, match="No .agentwire.tasks.yml"):
            load_task(project_dir, "lint")


# --- list_tasks ---

