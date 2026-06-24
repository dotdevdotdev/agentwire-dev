"""Tests for agentwire-owned GLOBAL skill install/drift (issue #475).

Global skills (currently just `/wiki`) were hand-placed at wiki-setup and never
resynced, so a stale or missing copy rotted invisibly. These tests cover the
drift-aware symlink install + doctor-facing drift report. Everything runs against
monkeypatched temp dirs — the real ~/.claude/skills/ is never touched.
"""

import shutil
from pathlib import Path

import pytest

import agentwire.__main__ as m
from agentwire.__main__ import (
    _managed_global_skills,
    _managed_skill_state,
    install_skills,
    skill_drift,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake packaged-source skills dir and a fake ~/.claude/skills target dir."""
    source = tmp_path / "pkg" / "skills"
    (source / "wiki").mkdir(parents=True)
    (source / "wiki" / "SKILL.md").write_text("# wiki skill\n")

    target_root = tmp_path / "claude" / "skills"

    monkeypatch.setattr(m, "CLAUDE_SKILLS_DIR", target_root)
    monkeypatch.setattr(m, "get_skills_source", lambda: source)
    return source, target_root


def test_managed_global_skills_is_just_wiki():
    assert _managed_global_skills() == ["wiki"]


def test_install_symlinks_fresh(env):
    source, target_root = env
    results = install_skills()
    assert results == {"wiki": "installed"}

    target = target_root / "wiki"
    assert target.is_symlink()
    assert target.resolve() == (source / "wiki").resolve()


def test_install_replaces_real_dir_copy(env):
    """The pre-#475 state: ~/.claude/skills/wiki is a REAL directory."""
    source, target_root = env
    target = target_root / "wiki"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# stale hand-placed copy\n")

    assert _managed_skill_state(target, source / "wiki") == "stale"

    results = install_skills()
    assert results == {"wiki": "updated"}
    assert target.is_symlink()
    assert target.resolve() == (source / "wiki").resolve()


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


def test_install_is_idempotent(env):
    assert install_skills() == {"wiki": "installed"}
    assert install_skills() == {"wiki": "current"}


def test_install_copy_mode(env):
    source, target_root = env
    results = install_skills(copy=True)
    assert results == {"wiki": "installed"}

    target = target_root / "wiki"
    assert not target.is_symlink()
    assert target.is_dir()
    assert (target / "SKILL.md").read_text() == "# wiki skill\n"


def test_install_missing_source(env):
    source, _ = env
    shutil.rmtree(source / "wiki")
    assert install_skills() == {"wiki": "missing-source"}


def test_skill_drift_ok_stale_missing(env):
    source, target_root = env

    # missing
    assert skill_drift() == {"wiki": "missing"}

    # ok after install
    install_skills()
    assert skill_drift() == {"wiki": "ok"}

    # stale when replaced with a real dir
    target = target_root / "wiki"
    target.unlink()
    target.mkdir()
    assert skill_drift() == {"wiki": "stale"}


def test_skill_drift_missing_when_no_source(env, monkeypatch):
    def boom():
        raise FileNotFoundError("no skills dir")

    monkeypatch.setattr(m, "get_skills_source", boom)
    assert skill_drift() == {"wiki": "missing"}
