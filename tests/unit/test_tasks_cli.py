"""Tests for agentwire/tasks_cli.py — propose-and-promote for .agentwire.tasks.yml (#720)."""

import argparse
import json

import pytest

from agentwire.tasks_cli import cmd_tasks_promote, cmd_tasks_review


def _ns(**kwargs):
    defaults = {"session": None, "json": False, "yes": False}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_proposed(proj, text):
    (proj / ".agentwire.tasks.proposed.yml").write_text(text)


class TestTasksReview:
    def test_no_draft_fails(self, proj, capsys):
        rc = cmd_tasks_review(_ns())
        assert rc == 1
        assert "No staged draft" in capsys.readouterr().err

    def test_valid_draft_shows_diff_and_commands(self, proj, capsys):
        _write_proposed(proj, "tasks:\n  t:\n    prompt: hi\n    post:\n      - echo done\n")
        rc = cmd_tasks_review(_ns())
        out = capsys.readouterr().out
        assert rc == 0
        assert "t.post[0]: echo done" in out
        assert "No validation issues" in out

    def test_invalid_draft_reports_issues(self, proj, capsys):
        _write_proposed(proj, "tasks:\n  bad:\n    retries: -1\n")
        rc = cmd_tasks_review(_ns())
        out = capsys.readouterr().out
        assert rc == 1
        assert "missing required 'prompt'" in out

    def test_invalid_yaml_reported(self, proj, capsys):
        _write_proposed(proj, "tasks: [this is not: valid: yaml\n")
        rc = cmd_tasks_review(_ns())
        assert rc == 1
        assert "Invalid YAML" in capsys.readouterr().err

    def test_json_mode(self, proj, capsys):
        _write_proposed(proj, "tasks:\n  t:\n    prompt: hi\n")
        rc = cmd_tasks_review(_ns(json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["success"] is True
        assert data["validation_issues"] == []


class TestTasksPromote:
    def test_no_draft_fails(self, proj, capsys):
        rc = cmd_tasks_promote(_ns(yes=True))
        assert rc == 1
        assert "No staged draft" in capsys.readouterr().err

    def test_promote_with_yes_writes_live_file_and_removes_draft(self, proj):
        _write_proposed(proj, "tasks:\n  t:\n    prompt: hi\n")
        rc = cmd_tasks_promote(_ns(yes=True))
        assert rc == 0
        assert (proj / ".agentwire.tasks.yml").exists()
        assert not (proj / ".agentwire.tasks.proposed.yml").exists()
        assert (proj / ".agentwire.tasks.yml").read_text() == "tasks:\n  t:\n    prompt: hi\n"

    def test_promote_without_yes_and_no_tty_refuses(self, proj, monkeypatch, capsys):
        _write_proposed(proj, "tasks:\n  t:\n    prompt: hi\n")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        rc = cmd_tasks_promote(_ns(yes=False))
        assert rc == 1
        assert not (proj / ".agentwire.tasks.yml").exists()
        assert (proj / ".agentwire.tasks.proposed.yml").exists()  # draft untouched

    def test_promote_json_mode_without_yes_refuses(self, proj, capsys):
        _write_proposed(proj, "tasks:\n  t:\n    prompt: hi\n")
        rc = cmd_tasks_promote(_ns(json=True, yes=False))
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert data["success"] is False

    def test_promote_refuses_invalid_draft(self, proj, capsys):
        _write_proposed(proj, "tasks:\n  bad:\n    retries: -1\n")
        rc = cmd_tasks_promote(_ns(yes=True))
        assert rc == 1
        assert not (proj / ".agentwire.tasks.yml").exists()
        assert (proj / ".agentwire.tasks.proposed.yml").exists()

    def test_promote_gitignores_the_live_file(self, proj):
        import subprocess
        subprocess.run(["git", "init"], cwd=proj, capture_output=True, check=True)
        _write_proposed(proj, "tasks:\n  t:\n    prompt: hi\n")
        rc = cmd_tasks_promote(_ns(yes=True))
        assert rc == 0
        gitignore = (proj / ".gitignore").read_text()
        assert ".agentwire.tasks*.yml" in gitignore
