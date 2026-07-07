"""Tests for the doctor task-migration check (#736).

The #720/#721 task-split relocated where tasks are READ from
(.agentwire.tasks.yml) without migrating existing inline .agentwire.yml
`tasks:` data, so those projects' scheduled/ensure tasks silently failed
(exit 6). Doctor must surface exactly that state.
"""

import agentwire.projects as projects_mod
from agentwire.doctor_cli import (
    _find_unmigrated_task_projects,
    _render_task_migration_section,
)


def _make_project(tmp_path, name, *, inline_tasks: bool, migrated: bool):
    """Create a fake local project dir and return its get_projects() dict."""
    proj_dir = tmp_path / name
    proj_dir.mkdir()
    cfg = "posture: bypass\n"
    if inline_tasks:
        cfg += "tasks:\n  daily:\n    prompt: do the thing\n"
    (proj_dir / ".agentwire.yml").write_text(cfg)
    if migrated:
        (proj_dir / ".agentwire.tasks.yml").write_text(
            "tasks:\n  daily:\n    prompt: do the thing\n"
        )
    return {"name": name, "path": str(proj_dir), "machine": "local"}


def _patch_projects(monkeypatch, project_dicts):
    monkeypatch.setattr(projects_mod, "get_projects", lambda machine=None: project_dicts)


def test_flags_unmigrated_project(tmp_path, monkeypatch):
    p = _make_project(tmp_path, "daily-book-report", inline_tasks=True, migrated=False)
    _patch_projects(monkeypatch, [p])
    assert _find_unmigrated_task_projects() == ["daily-book-report"]


def test_clean_once_migrated(tmp_path, monkeypatch):
    p = _make_project(tmp_path, "daily-book-report", inline_tasks=True, migrated=True)
    _patch_projects(monkeypatch, [p])
    assert _find_unmigrated_task_projects() == []


def test_project_without_inline_tasks_not_flagged(tmp_path, monkeypatch):
    p = _make_project(tmp_path, "no-tasks", inline_tasks=False, migrated=False)
    _patch_projects(monkeypatch, [p])
    assert _find_unmigrated_task_projects() == []


def test_mixed_fleet_reports_only_unmigrated(tmp_path, monkeypatch):
    projects = [
        _make_project(tmp_path, "broken", inline_tasks=True, migrated=False),
        _make_project(tmp_path, "fixed", inline_tasks=True, migrated=True),
        _make_project(tmp_path, "plain", inline_tasks=False, migrated=False),
    ]
    _patch_projects(monkeypatch, projects)
    assert _find_unmigrated_task_projects() == ["broken"]


def test_render_section_flags_and_counts(tmp_path, monkeypatch, capsys):
    p = _make_project(tmp_path, "daily-book-report", inline_tasks=True, migrated=False)
    _patch_projects(monkeypatch, [p])
    count = _render_task_migration_section()
    out = capsys.readouterr().out
    assert count == 1
    assert "daily-book-report" in out
    assert "will NOT run under ensure/scheduler" in out
    assert "agentwire tasks migrate" in out


def test_render_section_ok_when_clean(tmp_path, monkeypatch, capsys):
    p = _make_project(tmp_path, "fixed", inline_tasks=True, migrated=True)
    _patch_projects(monkeypatch, [p])
    count = _render_task_migration_section()
    assert count == 0
    assert "No projects with un-migrated inline tasks" in capsys.readouterr().out
