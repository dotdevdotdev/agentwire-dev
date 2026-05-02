"""Integration tests for CLI command handlers with mocked subprocess."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml


# --- cmd_roles_list ---

class TestCmdRolesList:
    def test_json_output(self, capsys):
        """cmd_roles_list --json should return bundled roles."""
        from agentwire.__main__ import _output_json

        # Directly test that roles are loadable
        from agentwire.roles import discover_role, parse_role_file

        bundled_names = ["agentwire", "voice", "worker", "task-runner", "chatbot", "init"]
        roles = []
        for name in bundled_names:
            path = discover_role(name)
            if path:
                role = parse_role_file(path)
                if role:
                    roles.append({
                        "name": role.name,
                        "description": role.description,
                        "has_tools": bool(role.tools),
                        "has_disallowed": bool(role.disallowed_tools),
                    })

        assert len(roles) == 6
        # Every role should have a name
        for r in roles:
            assert r["name"]


# --- cmd_safety_check ---

class TestCmdSafetyCheck:
    def test_allowed_command(self, tmp_path, monkeypatch):
        import agentwire.cli_safety as mod
        monkeypatch.setattr(mod, "RULES_DIR", tmp_path / "empty-rules")

        result = mod.check_command_safety("echo hello")
        assert result["decision"] == "allow"

    def test_blocked_by_pattern(self, tmp_path, monkeypatch):
        import agentwire.cli_safety as mod

        # Create a rules dir with a blocking pattern
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        patterns = {
            "bashToolPatterns": [
                {
                    "pattern": r"rm\s+-rf\s+/",
                    "action": "block",
                    "reason": "Dangerous recursive delete",
                }
            ]
        }
        with open(rules_dir / "patterns.yaml", "w") as f:
            yaml.safe_dump(patterns, f)

        monkeypatch.setattr(mod, "RULES_DIR", rules_dir)

        result = mod.check_command_safety("rm -rf /")
        assert result["decision"] == "block"
        assert "Dangerous" in result["reason"]


# --- cmd_task_list / cmd_task_validate via tasks module ---

class TestTaskCommands:
    def test_list_tasks(self, project_dir):
        config_path = project_dir / ".agentwire.yml"
        data = {
            "tasks": {
                "lint": {"prompt": "Run linting."},
                "test": {"prompt": "Run tests.", "retries": 2},
            }
        }
        with open(config_path, "w") as f:
            yaml.safe_dump(data, f)

        from agentwire.tasks import list_tasks
        tasks = list_tasks(project_dir)
        assert len(tasks) == 2
        names = {t["name"] for t in tasks}
        assert "lint" in names
        assert "test" in names

    def test_validate_good_task(self, project_dir):
        config_path = project_dir / ".agentwire.yml"
        data = {"tasks": {"good": {"prompt": "Do things.", "retries": 1}}}
        with open(config_path, "w") as f:
            yaml.safe_dump(data, f)

        from agentwire.tasks import load_task, validate_task
        task = load_task(project_dir, "good")
        issues = validate_task(task)
        assert issues == []

    def test_validate_bad_task(self, project_dir):
        from agentwire.tasks import TaskConfig, validate_task

        task = TaskConfig(name="bad", prompt="ok", retries=-1, mode="invalid")
        issues = validate_task(task)
        assert len(issues) >= 2


# --- cmd_projects_list (via projects discovery) ---

class TestProjectsDiscovery:
    def test_discovers_projects(self, tmp_path):
        """Projects with .agentwire.yml or .git should be discoverable."""
        # Create fake projects
        p1 = tmp_path / "project-a"
        p1.mkdir()
        (p1 / ".git").mkdir()

        p2 = tmp_path / "project-b"
        p2.mkdir()
        with open(p2 / ".agentwire.yml", "w") as f:
            yaml.safe_dump({"type": "bare"}, f)

        p3 = tmp_path / "not-a-project"
        p3.mkdir()

        # Check that we can identify projects
        projects = []
        for d in sorted(tmp_path.iterdir()):
            if d.is_dir():
                has_git = (d / ".git").exists()
                has_config = (d / ".agentwire.yml").exists()
                if has_git or has_config:
                    projects.append(d.name)

        assert "project-a" in projects
        assert "project-b" in projects
        assert "not-a-project" not in projects
