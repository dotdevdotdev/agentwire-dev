"""Tests for task inspection CLI paths — `task show` and `ensure --dry-run`.

Regression coverage for #278: OutputConfig lost its `notify` field but the
inspection/preview code paths kept reading `task.output.notify`, crashing
with AttributeError. Real runs were unaffected, so only these paths can rot
silently — exercise them against a fixture task.
"""

import argparse
import json

import pytest

from agentwire.ensure_cli import cmd_ensure, cmd_task_show


@pytest.fixture
def project_dir(tmp_path):
    """Project with a task that defines output capture + save."""
    (tmp_path / ".agentwire.yml").write_text("type: claude-bypass\n")
    (tmp_path / ".agentwire.tasks.yml").write_text(
        """
tasks:
  hello:
    prompt: "say hello"
    output:
      capture: 20
      save: /tmp/hello-output.txt
"""
    )
    return tmp_path


def _ns(**kwargs):
    return argparse.Namespace(**kwargs)


class TestTaskShow:
    def test_text_output(self, project_dir, monkeypatch, capsys):
        monkeypatch.chdir(project_dir)
        rc = cmd_task_show(_ns(task="hello", json=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Task: hello" in out
        assert "Save to: /tmp/hello-output.txt" in out

    def test_json_output(self, project_dir, monkeypatch, capsys):
        monkeypatch.chdir(project_dir)
        rc = cmd_task_show(_ns(task="hello", json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["name"] == "hello"
        assert data["output"] == {"capture": 20, "save": "/tmp/hello-output.txt"}


class TestEnsureDryRun:
    def test_dry_run(self, project_dir, capsys):
        rc = cmd_ensure(
            _ns(
                session="test-task-cli-278",
                task="hello",
                project=str(project_dir),
                dry_run=True,
                json=False,
            )
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "=== DRY RUN ===" in out
        assert "Save output to: /tmp/hello-output.txt" in out
