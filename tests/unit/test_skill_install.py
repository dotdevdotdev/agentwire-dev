"""Tests for agentwire-owned GLOBAL skill install/drift (issue #475).

Global skills (currently just `/wiki`) were hand-placed at wiki-setup and never
resynced, so a stale or missing copy rotted invisibly. These tests cover the
drift-aware symlink install + doctor-facing drift report. Everything runs against
monkeypatched temp dirs — the real ~/.claude/skills/ is never touched.
"""

import shutil

import pytest

import agentwire.__main__ as m
from agentwire.__main__ import (
    _managed_global_skills,
    _managed_skill_state,
    install_hooks,
    install_skills,
    skill_drift,
)
from agentwire.doctor_cli import _render_skill_section


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


def test_skill_drift_source_unavailable_when_no_source(env, monkeypatch):
    """A source checkout (skill only in the built wheel) → source-unavailable,
    NOT missing — so doctor doesn't false-positive."""
    def boom():
        raise FileNotFoundError("no skills dir")

    monkeypatch.setattr(m, "get_skills_source", boom)
    assert skill_drift() == {"wiki": "source-unavailable"}


def test_install_force_resymlinks_when_ok(env):
    """state 'ok' + force → still re-creates the symlink (updated, not current)."""
    source, target_root = env
    install_skills()
    assert _managed_skill_state(target_root / "wiki", source / "wiki") == "ok"

    results = install_skills(force=True)
    assert results == {"wiki": "updated"}
    assert (target_root / "wiki").resolve() == (source / "wiki").resolve()


# --- doctor wiring (issue #475 fix: source-unavailable must not be an issue) ---

def test_doctor_section_flags_missing_and_increments(env):
    """Source resolvable + target absent → doctor reports an issue."""
    assert _render_skill_section() == 1


def test_doctor_section_flags_stale_and_increments(env):
    """Source resolvable + real-dir target → stale → doctor reports an issue."""
    _, target_root = env
    target = target_root / "wiki"
    target.mkdir(parents=True)
    assert _render_skill_section() == 1


def test_doctor_section_clean_when_installed(env):
    install_skills()
    assert _render_skill_section() == 0


def test_doctor_section_no_issue_when_source_unavailable(env, monkeypatch):
    """Running from a checkout (no packaged source) must NOT bump issues_found."""
    def boom():
        raise FileNotFoundError("no skills dir")

    monkeypatch.setattr(m, "get_skills_source", boom)
    assert _render_skill_section() == 0


# --- install_hooks() actually drives install_skills() ---

def test_install_hooks_installs_skills(env, tmp_path, monkeypatch):
    """install_hooks() must heal global skills on the same pass."""
    # Stub out the hook half so we exercise only the skill wiring, and keep
    # damage-control healing from touching the real machine.
    monkeypatch.setattr(m, "get_hooks_source", lambda: tmp_path / "no-hooks")
    import agentwire.cli_safety as cs
    monkeypatch.setattr(cs, "heal_damage_control", lambda quiet=True: None)

    _, target_root = env
    target = target_root / "wiki"
    assert not target.exists()

    results = install_hooks()
    assert results.get("wiki") == "installed"
    assert target.is_symlink()
