"""Tests for agentwire-owned GLOBAL skill install/drift (issue #475).

Global skills (currently just `/wiki`) were hand-placed at wiki-setup and never
resynced, so a stale or missing copy rotted invisibly. These tests cover the
drift-aware symlink install + doctor-facing drift report. Everything runs against
monkeypatched temp dirs — the real ~/.claude/skills/ is never touched.
"""


import pytest

import agentwire.hooks_cli as m
from agentwire.doctor_cli import _render_skill_section
from agentwire.hooks_cli import (
    _managed_skill_state,
    install_skills,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake packaged-source skills dir and a fake ~/.claude/skills target dir."""
    source = tmp_path / "pkg" / "skills"
    (source / "wiki").mkdir(parents=True)
    (source / "wiki" / "SKILL.md").write_text("# wiki skill\n")

    target_root = tmp_path / "claude" / "skills"

    monkeypatch.setattr(m, "claude_skills_dir", lambda: target_root)
    monkeypatch.setattr(m, "get_skills_source", lambda: source)
    return source, target_root








def test_install_heals_wrong_symlink(env, tmp_path):
    source, target_root = env
    bogus = tmp_path / "somewhere-else"
    bogus.mkdir()
    target_root.mkdir(parents=True)
    target = target_root / "wiki"
    target.symlink_to(bogus, target_is_directory=True)

    assert _managed_skill_state(target, source / "wiki") == "stale"

    results = install_skills()
    assert results == {"wiki": "updated"}
    assert target.resolve() == (source / "wiki").resolve()














# --- doctor wiring (issue #475 fix: source-unavailable must not be an issue) ---

def test_doctor_section_flags_missing_and_increments(env):
    """Source resolvable + target absent → doctor reports an issue."""
    assert _render_skill_section() == 1








# --- install_hooks() actually drives install_skills() ---

