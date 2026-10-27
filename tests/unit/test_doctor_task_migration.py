"""Tests for the doctor task-migration check (#736).

The #720/#721 task-split relocated where tasks are READ from
(.agentwire.tasks.yml) without migrating existing inline .agentwire.yml
`tasks:` data, so those projects' scheduled/ensure tasks silently failed
(exit 6). Doctor must surface exactly that state.
"""

import agentwire.projects as projects_mod
from agentwire.doctor_cli import (
    _find_unmigrated_task_projects,
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










