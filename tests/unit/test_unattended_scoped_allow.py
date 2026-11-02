"""Path-scoped unattended grants (#914).

The property under test is NOT "the allowed case works". It is: for every
scoped grant, the OUT-OF-SCOPE case is still refused. Each positive case has a
companion negative one that differs only in where the command acts.

Driven end to end as the hook does it: ``check_command`` against the BUNDLED
rule set yields the rule id the command reached, then ``unattended_grant_allows``
decides the grant — because a synthetic one-rule config proves the fixture's
shape, not the behaviour.
"""

import json
from pathlib import Path

import pytest

from damage_control import engine as _core
from damage_control.engine import (
    DEFAULT_UNATTENDED_ALLOW,
    check_command,
    load_config,
    resolve_unattended_grants,
    unattended_grant_allows,
)
from damage_control.engine import bundled_rule_sources, bundled_tooldefs_dir

BUNDLED_RULES = bundled_rule_sources()
BUNDLED_TOOLDEFS = bundled_tooldefs_dir()


@pytest.fixture(scope="module")
def bundled_config():
    cfg = load_config(BUNDLED_RULES, BUNDLED_TOOLDEFS)
    cfg["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return cfg


@pytest.fixture
def store(tmp_path):
    """A stand-in memory store, plus an out-of-scope repo beside it."""
    s = tmp_path / "projects" / "proj-a" / "memory"
    s.mkdir(parents=True)
    other = tmp_path / "elsewhere" / "agentwire-dev"
    other.mkdir(parents=True)
    return s, other, f"{tmp_path}/projects/*/memory/"


def _grant(command, cwd, grants, config):
    result = check_command(command, config)
    assert result["decision"] == "ask" and result["id"] == "git.commit", (command, result)
    return unattended_grant_allows(result["id"], command, grants, str(cwd), result["pattern"])


def test_scoped_grant_allows_in_scope_and_refuses_the_same_command_elsewhere(bundled_config, store):
    s, other, scope = store
    grants = {"git.commit": [[scope]]}
    ok, why = _grant("git commit -m 'x'", s, grants, bundled_config)
    assert ok, why
    ok, why = _grant("git commit -m 'x'", other, grants, bundled_config)
    assert not ok and str(other) in why


def test_acting_on_a_repo_outside_the_scope_is_refused_in_every_spelling(bundled_config, store):
    """THE OPERATION — "the command acts on a repository other than the scoped
    one" — with spellings beneath it (#913's lesson: a rule that matches one
    phrasing misses its neighbour). Run from INSIDE the scope every time."""
    s, other, scope = store
    grants = {"git.commit": [[scope]]}
    spellings = [
        f"GIT_DIR={other}/.git git commit -m x",
        f"GIT_WORK_TREE={other} git commit -m x",
        f"env GIT_DIR={other}/.git git commit -m x",
        f"cd {other} && git commit -m x",
        f"git --git-dir={other}/.git commit -m x",
        f"git -C {other} commit -m x",
        f"git --work-tree={other} commit -m x",
        f"git --git-dir={s}/.git --work-tree={other} commit -m x",
        f"cd {s} && git -C {other} commit -m x",
        f"git -C {s}/../../.. commit -m x",
        f"if true; then cd {other} && git commit -m x; fi",  # compound body (#1112)
    ]
    granted = [c for c in spellings if _grant(c, s, grants, bundled_config)[0]]
    assert not granted, granted


def test_a_symlink_out_of_the_scope_is_not_in_scope(bundled_config, store):
    """The grantee can `ln -s` inside its scope, so a textual check is one
    symlink from meaningless."""
    s, other, scope = store
    (s / "escape").symlink_to(other)
    ok, why = _grant(f"git -C {s / 'escape'} commit -m x", s, {"git.commit": [[scope]]}, bundled_config)
    assert not ok and str(other) in why


def test_a_scope_over_a_repo_subdirectory_does_not_grant_over_the_repo(bundled_config, tmp_path):
    """git walks UP to find its repo; scope `<repo>/inner` must not grant `<repo>`."""
    repo = tmp_path / "outer"
    inner = repo / "inner"
    inner.mkdir(parents=True)
    (repo / ".git").mkdir()
    ok, why = _grant("git commit -m x", inner, {"git.commit": [[f"{inner}/"]]}, bundled_config)
    assert not ok and str(repo) in why
    ok, why = _grant("git commit -m x", repo, {"git.commit": [[f"{repo}/"]]}, bundled_config)
    assert ok, why


def test_a_core_worktree_redirect_cannot_carry_a_commit_out_of_scope(bundled_config, store):
    """#927: redirect an in-scope repo's worktree elsewhere, then commit
    entirely within scope. The commit must not be granted."""
    s, other, scope = store
    (s / ".git").mkdir()
    (s / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (s / ".git" / "config").write_text(f"[core]\n\tworktree = {other}\n")
    ok, why = _grant("git commit -m x", s, {"git.commit": [[scope]]}, bundled_config)
    assert not ok and str(other) in why


def test_a_malformed_scope_does_not_fall_back_to_the_unscoped_default(monkeypatch):
    """A typo in a scope path must not hand the task the unscoped default — a
    typo silently granting commits in EVERY repo."""
    assert "git.commit" in DEFAULT_UNATTENDED_ALLOW
    monkeypatch.setenv(
        "AGENTWIRE_UNATTENDED_ALLOW",
        json.dumps([{"id": "git.commit", "paths": ["relative/dir"]}]),
    )
    grants = resolve_unattended_grants({})
    ok, why = unattended_grant_allows(
        "git.commit", "git commit -m x", grants, "/anywhere", r"\bgit\s+commit\b"
    )
    assert not ok and "malformed" in why


def test_a_grant_never_downgrades_a_hard_block(bundled_config):
    """Grants resolve the `ask` tier only; `block` never reaches them."""
    granted = {**bundled_config, "safety": {**bundled_config["safety"],
                                            "unattended_allow": ["git.git-push-force-use-force-with-lease"]}}
    weak = [c for c in ("git -C /repo push --force origin main", "git reset --hard origin/main")
            if check_command(c, granted)["decision"] != "block"]
    assert not weak, weak
