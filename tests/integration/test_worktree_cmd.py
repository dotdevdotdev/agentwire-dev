"""Integration tests for `agentwire worktree` git mechanics (#307).

Exercises base-branch derivation, naming templates, monorepo project
inference, and the local branch↔session registry — with the tmux/session
launch stubbed out so the tests stay hermetic.
"""

import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from agentwire import __main__ as m
from agentwire import worktree_registry as reg
from agentwire.config import Config, WorktreeConfig


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def _origin_and_clone(tmp_path, default_branch="develop"):
    """A bare-ish origin (real repo) + a clone whose origin/HEAD → default_branch."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", default_branch)
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / "README.md").write_text("hi\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "base")

    clone = tmp_path / "clone-repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   capture_output=True, text=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    return origin, clone


@pytest.fixture
def wt_env(tmp_path, monkeypatch):
    """Isolate registry + stub session launch + capture the cmd_new call."""
    monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path / "registry")

    launched = {}

    def fake_cmd_new(ns):
        launched["args"] = ns
        return 0

    monkeypatch.setattr(m, "cmd_new", fake_cmd_new)
    monkeypatch.setattr(m, "_check_tmux_installed", lambda: True)
    monkeypatch.setattr(m, "tmux_session_exists", lambda *_: False)
    return launched


def _config(worktree_dir, **wt):
    cfg = Config()
    cfg.worktree = WorktreeConfig(worktree_dir=worktree_dir, **wt)
    return cfg


def _run(monkeypatch, cfg, **arg_overrides):
    monkeypatch.setattr(m, "load_config", lambda *a, **k: cfg, raising=False)
    # cmd_worktree imports the typed loader lazily from agentwire.config.
    import agentwire.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    base = dict(
        name=None, base=None, current=False, existing=False, ref=None,
        project=None, list=False, remove=False, prune=False, all=False,
        json=True, type=None, posture=None, harness=None, model=None,
        roles=None, env=None,
    )
    base.update(arg_overrides)
    return m.cmd_worktree(Namespace(**base))


def test_default_base_is_repo_derived(tmp_path, monkeypatch, wt_env):
    """No --base, no config default → branches off origin/HEAD (develop)."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone))
    assert rc == 0

    wt_path = wt_dir / "clone-repo-fix-bug"
    assert wt_path.exists()
    # New branch's parent is origin/develop's tip.
    base_sha = _git(clone, "rev-parse", "origin/develop").stdout.strip()
    parent = _git(wt_path, "rev-parse", "HEAD~0").stdout.strip()
    assert parent == base_sha
    # Registry recorded it with the derived base.
    entries = reg.entries(clone.resolve())
    assert len(entries) == 1
    assert entries[0]["base"] == "develop"
    assert entries[0]["session"] == "clone-repo-fix-bug"


def test_base_flag_overrides(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    # Add a second branch on origin to base off of.
    origin = tmp_path / "origin"
    _git(origin, "branch", "release")
    _git(clone, "fetch", "-q", "origin")

    wt_dir = tmp_path / "worktrees"
    rc = _run(monkeypatch, _config(wt_dir), name="hot", base="release", project=str(clone))
    assert rc == 0
    assert reg.entries(clone.resolve())[0]["base"] == "release"


def test_invalid_branch_name_fails_clean_no_orphan(tmp_path, monkeypatch, wt_env):
    """A name with spaces is rejected BEFORE any worktree lands on disk."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"

    rc = _run(monkeypatch, _config(wt_dir), name="Auth V2", project=str(clone))
    assert rc != 0
    # No orphaned worktree, nothing registered, nothing launched.
    assert not (wt_dir / "clone-repo-Auth-V2").exists()
    assert reg.entries(clone.resolve()) == []
    # git agrees there's only the main worktree.
    wt_list = _git(clone, "worktree", "list").stdout
    assert "clone-repo-Auth-V2" not in wt_list


def test_base_flag_wins_over_current(tmp_path, monkeypatch, wt_env):
    """--base X --current → --base wins (least-surprising)."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    origin = tmp_path / "origin"
    _git(origin, "branch", "release")
    _git(clone, "fetch", "-q", "origin")
    # Put the clone's current branch somewhere else entirely.
    _git(clone, "checkout", "-q", "-b", "scratch")

    rc = _run(monkeypatch, _config(wt_dir := tmp_path / "worktrees"),
              name="hot", base="release", current=True, project=str(clone))
    assert rc == 0
    assert reg.entries(clone.resolve())[0]["base"] == "release"


def test_naming_template_applied_to_branch(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir, naming="feature-{slug}")

    rc = _run(monkeypatch, cfg, name="Auth V2", project=str(clone))
    assert rc == 0
    # Branch is templated; session/worktree key stays the tmux-safe raw name.
    wt_path = wt_dir / "clone-repo-Auth-V2"  # spaces → '-' for tmux safety
    assert wt_path.exists()
    branch = _git(wt_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "feature-auth-v2"
    assert reg.entries(clone.resolve())[0]["branch"] == "feature-auth-v2"


def test_project_inferred_from_cwd_git_root(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    sub = clone / "packages" / "app"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    wt_dir = tmp_path / "worktrees"
    rc = _run(monkeypatch, _config(wt_dir), name="thing")  # no --project
    assert rc == 0
    assert (wt_dir / "clone-repo-thing").exists()
    assert reg.entries(clone.resolve())[0]["session"] == "clone-repo-thing"


def test_monorepo_many_sessions_one_repo(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    for n in ("one", "two", "three"):
        assert _run(monkeypatch, cfg, name=n, project=str(clone)) == 0

    sessions = {e["session"] for e in reg.entries(clone.resolve())}
    assert sessions == {"clone-repo-one", "clone-repo-two", "clone-repo-three"}
    for n in ("one", "two", "three"):
        assert (wt_dir / f"clone-repo-{n}").exists()


def test_remove_cleans_worktree_and_registry(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="kill-me", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo-kill-me"
    assert wt_path.exists()

    rc = _run(monkeypatch, cfg, name="kill-me", project=str(clone), remove=True)
    assert rc == 0
    assert not wt_path.exists()
    assert reg.entries(clone.resolve()) == []


def test_prune_drops_stale_entries(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="gone", project=str(clone)) == 0

    # Simulate an externally-removed worktree.
    wt_path = wt_dir / "clone-repo-gone"
    subprocess.run(["git", "-C", str(clone), "worktree", "remove", str(wt_path), "--force"],
                   capture_output=True)
    assert not wt_path.exists()
    assert len(reg.entries(clone.resolve())) == 1  # registry still has it

    rc = _run(monkeypatch, cfg, prune=True, project=str(clone))
    assert rc == 0
    assert reg.entries(clone.resolve()) == []
