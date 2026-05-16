"""Tests for agentwire/tasks.py — Task parsing, validation, loading."""

import pytest
import yaml
from pathlib import Path

from agentwire.tasks import (
    PreCommand,
    TaskConfig,
    OutputConfig,
    TaskNotFound,
    TaskValidationError,
    parse_pre_command,
    parse_task_config,
    validate_task,
    load_task,
    list_tasks,
)


# --- parse_pre_command ---

class TestParsePreCommand:
    def test_shorthand_string(self):
        pre = parse_pre_command("weather", "curl -s wttr.in")
        assert pre.name == "weather"
        assert pre.cmd == "curl -s wttr.in"
        assert pre.required is False
        assert pre.validate is None
        assert pre.timeout == 30

    def test_expanded_dict(self):
        config = {
            "cmd": "gcal-cli today --json",
            "required": True,
            "validate": "jq . > /dev/null",
            "timeout": 60,
        }
        pre = parse_pre_command("calendar", config)
        assert pre.name == "calendar"
        assert pre.cmd == "gcal-cli today --json"
        assert pre.required is True
        assert pre.validate == "jq . > /dev/null"
        assert pre.timeout == 60

    def test_expanded_dict_defaults(self):
        pre = parse_pre_command("data", {"cmd": "echo hi"})
        assert pre.required is False
        assert pre.validate is None
        assert pre.timeout == 30


# --- parse_task_config ---

class TestParseTaskConfig:
    def test_minimal_prompt_only(self):
        task = parse_task_config("lint", {"prompt": "Run linting."})
        assert task.name == "lint"
        assert task.prompt == "Run linting."
        assert task.retries == 0
        assert task.mode == "standard"
        assert task.pre == []
        assert task.post == []

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

    def test_shell_inheritance(self):
        task = parse_task_config("t", {"prompt": "go"}, default_shell="/bin/zsh")
        assert task.shell == "/bin/zsh"

    def test_shell_override(self):
        task = parse_task_config("t", {"prompt": "go", "shell": "/bin/fish"}, default_shell="/bin/zsh")
        assert task.shell == "/bin/fish"

    def test_missing_prompt_raises(self):
        with pytest.raises(TaskValidationError, match="missing required 'prompt'"):
            parse_task_config("bad", {})

    def test_empty_prompt_raises(self):
        with pytest.raises(TaskValidationError, match="missing required 'prompt'"):
            parse_task_config("bad", {"prompt": ""})


# --- validate_task ---

class TestValidateTask:
    def test_valid_returns_empty(self):
        task = TaskConfig(name="ok", prompt="Do stuff.")
        assert validate_task(task) == []

    def test_empty_prompt(self):
        task = TaskConfig(name="bad", prompt="   ")
        issues = validate_task(task)
        assert any("Empty prompt" in i for i in issues)

    def test_negative_retries(self):
        task = TaskConfig(name="bad", prompt="ok", retries=-1)
        issues = validate_task(task)
        assert any("Negative retry count" in i for i in issues)

    def test_negative_retry_delay(self):
        task = TaskConfig(name="bad", prompt="ok", retry_delay=-5)
        issues = validate_task(task)
        assert any("Negative retry delay" in i for i in issues)

    def test_invalid_idle_timeout(self):
        task = TaskConfig(name="bad", prompt="ok", idle_timeout=0)
        issues = validate_task(task)
        assert any("idle_timeout" in i for i in issues)

    def test_invalid_mode(self):
        task = TaskConfig(name="bad", prompt="ok", mode="turbo")
        issues = validate_task(task)
        assert any("Invalid mode" in i for i in issues)

    def test_bad_max_iterations(self):
        task = TaskConfig(name="bad", prompt="ok", max_iterations=0)
        issues = validate_task(task)
        assert any("max_iterations" in i for i in issues)

        task2 = TaskConfig(name="bad", prompt="ok", max_iterations=21)
        issues2 = validate_task(task2)
        assert any("max_iterations" in i for i in issues2)

    def test_negative_loop_delay(self):
        task = TaskConfig(name="bad", prompt="ok", loop_delay=-1)
        issues = validate_task(task)
        assert any("loop_delay" in i for i in issues)

    def test_empty_pre_command(self):
        task = TaskConfig(name="bad", prompt="ok", pre=[PreCommand(name="x", cmd="")])
        issues = validate_task(task)
        assert any("Empty command" in i for i in issues)


# --- load_task ---

class TestLoadTask:
    def test_load_from_yml(self, project_dir):
        config_path = project_dir / ".agentwire.yml"
        data = {
            "tasks": {
                "lint": {"prompt": "Run lint."},
                "test": {"prompt": "Run tests."},
            }
        }
        with open(config_path, "w") as f:
            yaml.safe_dump(data, f)

        task = load_task(project_dir, "lint")
        assert task.name == "lint"
        assert task.prompt == "Run lint."

    def test_task_not_found(self, project_dir):
        config_path = project_dir / ".agentwire.yml"
        data = {"tasks": {"lint": {"prompt": "Run lint."}}}
        with open(config_path, "w") as f:
            yaml.safe_dump(data, f)

        with pytest.raises(TaskNotFound, match="not found"):
            load_task(project_dir, "nonexistent")

    def test_no_config_file(self, tmp_path):
        with pytest.raises(TaskNotFound, match="No .agentwire.yml"):
            load_task(tmp_path, "anything")


# --- list_tasks ---

class TestListTasks:
    def test_lists_all(self, project_dir):
        config_path = project_dir / ".agentwire.yml"
        data = {
            "tasks": {
                "a": {"prompt": "p", "pre": {"x": "echo"}, "mode": "loop"},
                "b": {"prompt": "q", "post": ["echo done"]},
            }
        }
        with open(config_path, "w") as f:
            yaml.safe_dump(data, f)

        tasks = list_tasks(project_dir)
        assert len(tasks) == 2
        names = {t["name"] for t in tasks}
        assert names == {"a", "b"}

    def test_empty_when_no_tasks(self, project_dir):
        config_path = project_dir / ".agentwire.yml"
        with open(config_path, "w") as f:
            yaml.safe_dump({"type": "bare"}, f)

        assert list_tasks(project_dir) == []

    def test_empty_when_no_file(self, tmp_path):
        assert list_tasks(tmp_path) == []
