"""Integration tests for `agentwire worktree` git mechanics (#307).

Exercises base-branch derivation, naming templates, monorepo project
inference, and the local branch↔session registry — with the tmux/session
launch stubbed out so the tests stay hermetic.
"""

import json
import subprocess
from argparse import Namespace

import pytest

from agentwire import session_cli as m
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
    """Isolate registry + stub session launch + capture the cmd_new call.

    Also stubs `gh` away (shutil.which("gh") -> None) so branch-cleanup merge
    checks deterministically take the git merge-base fallback path instead of
    depending on whether the test host has `gh` installed/authenticated.
    """
    monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path / "registry")
    monkeypatch.setattr(m.chrome_tabs, "REGISTRY_FILE", tmp_path / "chrome-tabs.json")

    launched = {}

    def fake_cmd_new(ns):
        launched["args"] = ns
        return 0

    monkeypatch.setattr(m, "cmd_new", fake_cmd_new)
    monkeypatch.setattr(m, "_check_tmux_installed", lambda: True)
    monkeypatch.setattr(m, "tmux_session_exists", lambda *_: False)
    monkeypatch.setattr(m.shutil, "which", lambda *_: None)
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
        json=True, posture=None, model=None,
        roles=None, env=None, created_by=None, caller_session=None, kind=None,
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

    wt_path = wt_dir / "clone-repo" / "fix-bug"
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


def test_created_by_forwarded_to_cmd_new(tmp_path, monkeypatch, wt_env):
    """--created-by (the spawner) flows through to cmd_new so the worktree
    session records its creator → notify-parent / resolve_parent resolves (#578)."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone),
              created_by="orchestrator")
    assert rc == 0
    # The captured cmd_new Args carries the creator through unchanged.
    assert wt_env["args"].created_by == "orchestrator"


def test_caller_session_forwarded_to_cmd_new(tmp_path, monkeypatch, wt_env):
    """--caller-session (the MCP-forwarded candidate parent) flows through to
    cmd_new distinctly from --created-by, so cmd_new's own same-project check
    decides inheritance (#715) rather than cmd_worktree forcing it."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone),
              caller_session="orchestrator")
    assert rc == 0
    assert wt_env["args"].caller_session == "orchestrator"
    assert wt_env["args"].created_by is None


def test_default_kind_is_worker_with_explicit_worktree_topology(tmp_path, monkeypatch, wt_env):
    """#716: no --kind → cmd_new gets kind='worker' AND an explicit
    worktree_topology=True override. The override is load-bearing: the
    session_name _launch_session hands cmd_new is a flat `{project}-{name}`
    (no slash), so cmd_new's own bool(branch) re-derivation would silently
    read it as branchless and pick the WRONG (pane) etiquette + posture."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone))
    assert rc == 0
    assert wt_env["args"].kind == "worker"
    assert wt_env["args"].worktree_topology is True
    # And the session name really has no slash — this is what makes the
    # override load-bearing rather than redundant.
    assert "/" not in wt_env["args"].session


def test_kind_orchestrator_overrides_default(tmp_path, monkeypatch, wt_env):
    """--kind orchestrator flows through instead of the worker default."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone), kind="orchestrator")
    assert rc == 0
    assert wt_env["args"].kind == "orchestrator"


def test_kind_reviewer_overrides_default(tmp_path, monkeypatch, wt_env):
    """--kind reviewer flows through instead of the worker default (#827)."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone), kind="reviewer")
    assert rc == 0
    assert wt_env["args"].kind == "reviewer"


def test_kind_reviewer_created_by_stays_unresolved(tmp_path, monkeypatch, wt_env):
    """Unlike kind=orchestrator, reviewer does NOT get the '' rooting default
    forced here — cmd_worktree/_launch_session forwards created_by=None
    straight through for reviewer just like it does for worker, so cmd_new's
    own same-project inheritance (#715) decides, not a joint default (#827)."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone), kind="reviewer")
    assert rc == 0
    assert wt_env["args"].kind == "reviewer"
    assert wt_env["args"].created_by is None


def test_kind_orchestrator_passes_created_by_through_unresolved(tmp_path, monkeypatch, wt_env):
    """cmd_worktree/_launch_session does NOT resolve the joint rooting
    default itself — it just forwards kind + created_by=None through to
    cmd_new, which is the ONE place that default lives (SSOT — see
    TestCmdNewDefaultCreatedByRooting::test_explicit_kind_orchestrator_roots_even_same_project_caller
    in test_cli_commands.py for the actual '' resolution, end-to-end).
    cmd_new is mocked out in this fixture, so this only pins the pass-through
    contract, not the resolution itself."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone), kind="orchestrator")
    assert rc == 0
    assert wt_env["args"].kind == "orchestrator"
    assert wt_env["args"].created_by is None


def test_kind_worker_default_created_by_still_none(tmp_path, monkeypatch, wt_env):
    """The joint rooting default is specific to kind=orchestrator — the
    common worker case keeps created_by=None so #715's same-project
    inheritance logic still runs downstream in cmd_new."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone))
    assert rc == 0
    assert wt_env["args"].created_by is None


def test_explicit_created_by_wins_over_orchestrator_joint_default(tmp_path, monkeypatch, wt_env):
    """Explicit --created-by (including '') always wins over the joint
    default, whichever value it is — even a real parent name for an
    orchestrator that DOES want to inherit one."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    rc = _run(monkeypatch, cfg, name="fix-bug", project=str(clone),
              kind="orchestrator", created_by="some-parent")
    assert rc == 0
    assert wt_env["args"].created_by == "some-parent"


def test_orchestrator_sugar_verb_forces_kind(tmp_path, monkeypatch, wt_env):
    """`agentwire orchestrator` = `worktree --kind orchestrator` — cmd_orchestrator
    is a thin wrapper that forces args.kind before delegating to cmd_worktree.
    The joint rooting default itself resolves downstream in cmd_new (SSOT —
    see test_cli_commands.py), so this only pins that the sugar verb forces
    kind and forwards created_by unresolved, same as the plain verb above."""
    _, clone = _origin_and_clone(tmp_path)
    cfg = _config(tmp_path / "worktrees")

    base = dict(
        name="proj-window", base=None, current=False, existing=False, ref=None,
        project=str(clone), list=False, remove=False, prune=False, all=False,
        json=True, posture=None, model=None,
        roles=None, env=None, created_by=None, caller_session=None, kind=None,
    )
    monkeypatch.setattr(m, "load_config", lambda *a, **k: cfg, raising=False)
    import agentwire.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    rc = m.cmd_orchestrator(Namespace(**base))
    assert rc == 0
    assert wt_env["args"].kind == "orchestrator"
    assert wt_env["args"].created_by is None


def test_record_session_creator_persists_creator(tmp_path, monkeypatch):
    """The creator-registry mechanism cmd_new uses records the spawner so
    _display_parent (and resolve_parent) returns it for a worktree session."""
    from agentwire import core

    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path / "agentwire")
    monkeypatch.setattr(core, "get_parent_from_config", lambda *_a, **_k: None)

    core._record_session_creator("clone-repo-fix-bug", "orchestrator", via="worktree")
    assert core.load_session_metadata("clone-repo-fix-bug")["created_by"] == "orchestrator"
    assert core._display_parent("clone-repo-fix-bug") == "orchestrator"


def test_record_session_role_persists_and_merges_with_creator(tmp_path, monkeypatch):
    """#747 — role (orchestrator/worker) is a separate merge-preserving field
    alongside created_by, so the session_created broadcast can carry both."""
    from agentwire import core

    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path / "agentwire")

    core._record_session_creator("clone-repo-fix-bug", "orchestrator", via="worktree")
    core._record_session_role("clone-repo-fix-bug", "worker")

    metadata = core.load_session_metadata("clone-repo-fix-bug")
    assert metadata["created_by"] == "orchestrator"
    assert metadata["role"] == "worker"

    # A falsy role is a no-op — never clobbers an already-recorded role.
    core._record_session_role("clone-repo-fix-bug", None)
    assert core.load_session_metadata("clone-repo-fix-bug")["role"] == "worker"


def test_notify_portal_session_created_posts_enriched_payload(monkeypatch):
    """#747 — the creating process posts name/parent/role to /api/notify
    directly, rather than relying solely on the racy global tmux hook."""
    import json as _json
    import urllib.request

    from agentwire import core

    seen = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["url"] = req.full_url
        seen["payload"] = _json.loads(req.data.decode())
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    core.notify_portal_session_created("clone-repo-fix-bug", "orchestrator", "worker")

    assert seen["url"].endswith("/api/notify")
    assert seen["payload"] == {
        "event": "session_created",
        "session": "clone-repo-fix-bug",
        "parent": "orchestrator",
        "role": "worker",
    }


def test_notify_portal_session_created_swallows_failures(monkeypatch):
    """Fire-and-forget: the portal may not be running — never raise."""
    import urllib.request

    from agentwire import core

    def fail(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    core.notify_portal_session_created("proj", None, None)  # must not raise


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
    assert not (wt_dir / "clone-repo" / "Auth-V2").exists()
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

    rc = _run(monkeypatch, _config(tmp_path / "worktrees"),
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
    wt_path = wt_dir / "clone-repo" / "Auth-V2"  # spaces → '-' for tmux safety
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
    assert (wt_dir / "clone-repo" / "thing").exists()
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
        assert (wt_dir / "clone-repo" / n).exists()


def test_remove_cleans_worktree_and_registry(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="kill-me", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo" / "kill-me"
    assert wt_path.exists()

    rc = _run(monkeypatch, cfg, name="kill-me", project=str(clone), remove=True)
    assert rc == 0
    assert not wt_path.exists()
    assert reg.entries(clone.resolve()) == []
    # Last worktree gone → the empty per-project dir goes with it.
    assert not (wt_dir / "clone-repo").exists()
    assert wt_dir.exists()  # the root itself is never removed


def test_remove_keeps_project_dir_while_siblings_remain(tmp_path, monkeypatch, wt_env):
    """Removing one of two worktrees must NOT sweep the project dir."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="one", project=str(clone)) == 0
    assert _run(monkeypatch, cfg, name="two", project=str(clone)) == 0

    assert _run(monkeypatch, cfg, name="one", project=str(clone), remove=True) == 0
    assert not (wt_dir / "clone-repo" / "one").exists()
    assert (wt_dir / "clone-repo" / "two").exists()
    assert (wt_dir / "clone-repo").exists()

    assert _run(monkeypatch, cfg, name="two", project=str(clone), remove=True) == 0
    assert not (wt_dir / "clone-repo").exists()


def test_remove_by_full_session_name(tmp_path, monkeypatch, wt_env):
    """--remove accepts the full {project}-{name} session name too."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="fix-bug", project=str(clone)) == 0

    rc = _run(monkeypatch, cfg, name="clone-repo-fix-bug", project=str(clone), remove=True)
    assert rc == 0
    assert not (wt_dir / "clone-repo").exists()
    assert reg.entries(clone.resolve()) == []


# --- Per-project overrides via .agentwire.yml `worktree:` block (#705) ---

def _write_project_override(repo, **kv):
    import yaml
    (repo / ".agentwire.yml").write_text(yaml.safe_dump({"worktree": kv}))


def test_project_dir_override_creates_under_project_dir(tmp_path, monkeypatch, wt_env):
    """`worktree.dir` in .agentwire.yml moves the root for THIS repo only;
    the nesting shape <dir>/<project>/<name>/ is unchanged."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    global_dir = tmp_path / "global-worktrees"
    project_dir = tmp_path / "project-worktrees"
    _write_project_override(clone, dir=str(project_dir))

    rc = _run(monkeypatch, _config(global_dir), name="fix-bug", project=str(clone))
    assert rc == 0
    assert (project_dir / "clone-repo" / "fix-bug").exists()
    assert not global_dir.exists()
    # Registry records the RESOLVED path, so the entry survives override changes.
    entries = reg.entries(clone.resolve())
    assert entries[0]["worktree_path"] == str(project_dir / "clone-repo" / "fix-bug")


def test_project_dir_override_remove_round_trip(tmp_path, monkeypatch, wt_env):
    """--remove resolves through the same project-scoped dir as create."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    global_dir = tmp_path / "global-worktrees"
    project_dir = tmp_path / "project-worktrees"
    _write_project_override(clone, dir=str(project_dir))
    cfg = _config(global_dir)

    assert _run(monkeypatch, cfg, name="fix-bug", project=str(clone)) == 0
    wt_path = project_dir / "clone-repo" / "fix-bug"
    assert wt_path.exists()

    assert _run(monkeypatch, cfg, name="fix-bug", project=str(clone), remove=True) == 0
    assert not wt_path.exists()
    assert reg.entries(clone.resolve()) == []
    # Last worktree gone → empty per-project dir swept under the OVERRIDE root.
    assert not (project_dir / "clone-repo").exists()


def test_project_base_override_beats_global(tmp_path, monkeypatch, wt_env):
    """`worktree.base` in .agentwire.yml wins over config default_base."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    origin = tmp_path / "origin"
    _git(origin, "branch", "release")
    _git(clone, "fetch", "-q", "origin")
    _write_project_override(clone, base="release")

    wt_dir = tmp_path / "worktrees"
    rc = _run(monkeypatch, _config(wt_dir, default_base="develop"),
              name="hot", project=str(clone))
    assert rc == 0
    assert reg.entries(clone.resolve())[0]["base"] == "release"
    # The new branch actually forked from origin/release.
    base_sha = _git(clone, "rev-parse", "origin/release").stdout.strip()
    head = _git(wt_dir / "clone-repo" / "hot", "rev-parse", "HEAD").stdout.strip()
    assert head == base_sha


def test_invocation_base_beats_project_override(tmp_path, monkeypatch, wt_env):
    """Per-invocation --base is the most specific — beats the project block."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    origin = tmp_path / "origin"
    _git(origin, "branch", "release")
    _git(clone, "fetch", "-q", "origin")
    _write_project_override(clone, base="release")

    rc = _run(monkeypatch, _config(tmp_path / "worktrees"),
              name="hot", base="develop", project=str(clone))
    assert rc == 0
    assert reg.entries(clone.resolve())[0]["base"] == "develop"


def test_current_flag_beats_project_base(tmp_path, monkeypatch, wt_env):
    """--current is per-invocation too — beats the project block."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    origin = tmp_path / "origin"
    _git(origin, "branch", "release")
    _git(clone, "fetch", "-q", "origin")
    _write_project_override(clone, base="release")
    # Current branch differs from both develop and release... but must exist
    # on origin for the fetch — keep it on develop and just assert the pick.
    rc = _run(monkeypatch, _config(tmp_path / "worktrees"),
              name="hot", current=True, project=str(clone))
    assert rc == 0
    assert reg.entries(clone.resolve())[0]["base"] == "develop"  # the checked-out branch


def test_no_override_falls_through_to_global(tmp_path, monkeypatch, wt_env):
    """An .agentwire.yml WITHOUT a worktree block changes nothing — global
    dir/base apply as before."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    (clone / ".agentwire.yml").write_text("posture: bypass\n")

    global_dir = tmp_path / "global-worktrees"
    rc = _run(monkeypatch, _config(global_dir), name="fix-bug", project=str(clone))
    assert rc == 0
    assert (global_dir / "clone-repo" / "fix-bug").exists()
    assert reg.entries(clone.resolve())[0]["base"] == "develop"  # repo-derived


def test_entries_survive_override_change(tmp_path, monkeypatch, wt_env):
    """A worktree created under the old dir stays removable after the
    override changes: the registry stores the resolved path."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    old_dir = tmp_path / "old-trees"
    new_dir = tmp_path / "new-trees"
    _write_project_override(clone, dir=str(old_dir))
    cfg = _config(tmp_path / "global-worktrees")

    assert _run(monkeypatch, cfg, name="fix-bug", project=str(clone)) == 0
    wt_path = old_dir / "clone-repo" / "fix-bug"
    assert wt_path.exists()

    # Override moves — the already-created worktree must still resolve.
    _write_project_override(clone, dir=str(new_dir))
    assert _run(monkeypatch, cfg, name="fix-bug", project=str(clone), remove=True) == 0
    assert not wt_path.exists()
    assert reg.entries(clone.resolve()) == []


def test_prune_drops_stale_entries(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="gone", project=str(clone)) == 0

    # Simulate an externally-removed worktree.
    wt_path = wt_dir / "clone-repo" / "gone"
    subprocess.run(["git", "-C", str(clone), "worktree", "remove", str(wt_path), "--force"],
                   capture_output=True)
    assert not wt_path.exists()
    assert len(reg.entries(clone.resolve())) == 1  # registry still has it

    rc = _run(monkeypatch, cfg, prune=True, project=str(clone))
    assert rc == 0
    assert reg.entries(clone.resolve()) == []
    # Pruning the project's last worktree sweeps the empty per-project dir.
    assert not (wt_dir / "clone-repo").exists()


# --- Atomic teardown + branch cleanup (#717) ---

def _local_branch_exists(repo, branch):
    return bool(_git(repo, "branch", "--list", branch).stdout.strip())


def _remote_branch_exists(repo, branch):
    return bool(_git(repo, "ls-remote", "--heads", "origin", branch).stdout.strip())


def test_remove_fails_loudly_when_dir_survives(tmp_path, monkeypatch, wt_env, capsys):
    """A worktree git can't actually clear must fail LOUDLY — never silently
    'unregister' and leave the dir on disk (#717)."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="stuck", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo" / "stuck"
    assert wt_path.exists()

    # Break the worktree's link back to its admin dir so `git worktree
    # remove --force` fails, while its real content stays on disk.
    (wt_path / ".git").unlink()

    capsys.readouterr()
    rc = _run(monkeypatch, cfg, name="stuck", project=str(clone), remove=True)
    assert rc == 1
    assert wt_path.exists()  # never silently swept away
    assert len(reg.entries(clone.resolve())) == 1  # entry kept, not dropped

    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["worktree_removed"] is False
    assert payload["error"]


def test_remove_kills_alive_session(tmp_path, monkeypatch, wt_env, capsys):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="alive", project=str(clone)) == 0

    real_run = subprocess.run
    killed = []

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["tmux", "kill-session"]:
            killed.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(m, "tmux_session_exists", lambda name: name == "clone-repo-alive")
    monkeypatch.setattr(m.subprocess, "run", fake_run)

    capsys.readouterr()
    rc = _run(monkeypatch, cfg, name="alive", project=str(clone), remove=True)
    assert rc == 0
    assert killed == [["tmux", "kill-session", "-t", "clone-repo-alive"]]
    payload = json.loads(capsys.readouterr().out)
    assert payload["killed"] is True


def test_remove_deletes_trivially_merged_branch_local_and_remote(tmp_path, monkeypatch, wt_env):
    """A branch identical to its base (no new commits) is trivially safe to
    delete without gh or a real PR — exercises the git ancestor fallback."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="done", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo" / "done"
    _git(wt_path, "push", "-q", "-u", "origin", "done")
    assert _remote_branch_exists(clone, "done")

    rc = _run(monkeypatch, cfg, name="done", project=str(clone), remove=True)
    assert rc == 0
    assert not wt_path.exists()
    assert not _local_branch_exists(clone, "done")
    assert not _remote_branch_exists(clone, "done")


def test_remove_keeps_unmerged_branch_by_default(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="wip", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo" / "wip"
    (wt_path / "new.txt").write_text("still working\n")
    _git(wt_path, "add", "-A")
    _git(wt_path, "commit", "-qm", "wip commit")

    rc = _run(monkeypatch, cfg, name="wip", project=str(clone), remove=True)
    assert rc == 0
    assert not wt_path.exists()  # worktree still torn down
    assert _local_branch_exists(clone, "wip")  # branch preserved — unmerged work


def test_remove_force_delete_branch_removes_unmerged(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="wip2", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo" / "wip2"
    (wt_path / "new.txt").write_text("still working\n")
    _git(wt_path, "add", "-A")
    _git(wt_path, "commit", "-qm", "wip commit")

    rc = _run(monkeypatch, cfg, name="wip2", project=str(clone), remove=True, force_delete_branch=True)
    assert rc == 0
    assert not _local_branch_exists(clone, "wip2")


def test_remove_force_delete_branch_refuses_open_pr(tmp_path, monkeypatch, wt_env):
    """#756: --force-delete-branch alone must NOT close an OPEN PR by nuking
    its remote head branch — that's the surprising destruction the guard
    exists for. --close-pr-branch is the required, explicit override."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)

    def _make_wip_worktree(name):
        assert _run(monkeypatch, cfg, name=name, project=str(clone)) == 0
        wt_path = wt_dir / "clone-repo" / name
        (wt_path / "new.txt").write_text("still working\n")
        _git(wt_path, "add", "-A")
        _git(wt_path, "commit", "-qm", "wip commit")
        return wt_path

    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"state": "OPEN", "number": 99}', stderr="",
            )
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(m.shutil, "which", lambda *_: "/usr/bin/gh")
    monkeypatch.setattr(m.subprocess, "run", fake_run)

    wt_path_a = _make_wip_worktree("wip3a")
    rc = _run(monkeypatch, cfg, name="wip3a", project=str(clone), remove=True, force_delete_branch=True)
    assert rc == 0  # worktree teardown itself still succeeds
    assert not wt_path_a.exists()
    assert _local_branch_exists(clone, "wip3a")  # branch preserved — open PR guarded

    wt_path_b = _make_wip_worktree("wip3b")
    rc = _run(monkeypatch, cfg, name="wip3b", project=str(clone), remove=True,
              force_delete_branch=True, close_pr_branch=True)
    assert rc == 0
    assert not wt_path_b.exists()
    assert not _local_branch_exists(clone, "wip3b")  # explicit override deletes it


def test_remove_keep_branch_flag_preserves_merged_branch(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="keepme", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo" / "keepme"

    rc = _run(monkeypatch, cfg, name="keepme", project=str(clone), remove=True, keep_branch=True)
    assert rc == 0
    assert not wt_path.exists()
    assert _local_branch_exists(clone, "keepme")


def test_prune_gc_merged_tears_down_only_merged_worktrees(tmp_path, monkeypatch, wt_env):
    """--prune --gc-merged sweeps registered-but-still-present worktrees whose
    branch is confirmed merged; an in-flight (unmerged) worktree is untouched."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="finished", project=str(clone)) == 0
    assert _run(monkeypatch, cfg, name="ongoing", project=str(clone)) == 0

    ongoing_path = wt_dir / "clone-repo" / "ongoing"
    (ongoing_path / "wip.txt").write_text("still going\n")
    _git(ongoing_path, "add", "-A")
    _git(ongoing_path, "commit", "-qm", "wip")

    rc = _run(monkeypatch, cfg, prune=True, gc_merged=True, project=str(clone))
    assert rc == 0

    finished_path = wt_dir / "clone-repo" / "finished"
    assert not finished_path.exists()
    assert ongoing_path.exists()
    sessions = {e["session"] for e in reg.entries(clone.resolve())}
    assert sessions == {"clone-repo-ongoing"}


# --- claude-in-chrome tab crash backstop (#717) ---

def test_remove_reports_and_clears_orphaned_tabs(tmp_path, monkeypatch, wt_env):
    """A tab the session tracked but never closed itself must be surfaced (not
    silently dropped) by --remove, and cleared from the registry afterward."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="leaky", project=str(clone)) == 0
    m.chrome_tabs.track("clone-repo-leaky", "tab-123", url="http://localhost:3000")

    rc = _run(monkeypatch, cfg, name="leaky", project=str(clone), remove=True)
    assert rc == 0
    assert m.chrome_tabs.tabs_for("clone-repo-leaky") == []  # cleared, not left dangling


def test_remove_with_no_tracked_tabs_reports_none(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="clean", project=str(clone)) == 0

    rc = _run(monkeypatch, cfg, name="clean", project=str(clone), remove=True)
    assert rc == 0
    assert m.chrome_tabs.tabs_for("clone-repo-clean") == []


def test_remove_failure_keeps_tracked_tabs_untouched(tmp_path, monkeypatch, wt_env):
    """If teardown fails loudly (dir survives), tracked tabs stay tracked —
    they're only cleared once the session is actually gone."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="stuck2", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo" / "stuck2"
    (wt_path / ".git").unlink()
    m.chrome_tabs.track("clone-repo-stuck2", "tab-456")

    rc = _run(monkeypatch, cfg, name="stuck2", project=str(clone), remove=True)
    assert rc == 1
    assert len(m.chrome_tabs.tabs_for("clone-repo-stuck2")) == 1


def test_prune_gc_merged_clears_tabs_for_torn_down_sessions_only(tmp_path, monkeypatch, wt_env):
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="finished2", project=str(clone)) == 0
    assert _run(monkeypatch, cfg, name="ongoing2", project=str(clone)) == 0

    ongoing_path = wt_dir / "clone-repo" / "ongoing2"
    (ongoing_path / "wip.txt").write_text("still going\n")
    _git(ongoing_path, "add", "-A")
    _git(ongoing_path, "commit", "-qm", "wip")

    m.chrome_tabs.track("clone-repo-finished2", "tab-a")
    m.chrome_tabs.track("clone-repo-ongoing2", "tab-b")

    rc = _run(monkeypatch, cfg, prune=True, gc_merged=True, project=str(clone))
    assert rc == 0
    assert m.chrome_tabs.tabs_for("clone-repo-finished2") == []  # GC'd session's tab cleared
    assert len(m.chrome_tabs.tabs_for("clone-repo-ongoing2")) == 1  # untouched session's tab kept


# --- Regressions for #740 ---

def test_remove_resolves_project_name_from_cwd_inside_repo(tmp_path, monkeypatch, wt_env):
    """--project <name> (a bare name, not a path) must resolve via the
    configured projects dir, not by naively joining onto cwd. Before #740's
    fix, running from inside the repo turned `--project <name>` into
    `<cwd>/<name>` — a nonexistent doubled path — so removal failed after
    already having killed the session."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    cfg.projects.dir = tmp_path  # so bare name "clone-repo" resolves to `clone`

    assert _run(monkeypatch, cfg, name="kill-me", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo" / "kill-me"
    assert wt_path.exists()

    monkeypatch.chdir(clone)  # cwd IS the project root — the reported scenario
    rc = _run(monkeypatch, cfg, name="kill-me", project=clone.name, remove=True)
    assert rc == 0
    assert not wt_path.exists()
    assert reg.entries(clone.resolve()) == []


def test_remove_does_not_kill_session_when_worktree_removal_fails(tmp_path, monkeypatch, wt_env, capsys):
    """Teardown must not kill a live session if the worktree removal fails —
    doing so half-succeeds and leaves an orphaned worktree+branch with no
    session left to notice (#740)."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="stuck3", project=str(clone)) == 0
    wt_path = wt_dir / "clone-repo" / "stuck3"
    # Break the worktree's link back to its admin dir so `git worktree
    # remove --force` fails, while its real content stays on disk.
    (wt_path / ".git").unlink()

    killed = []
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["tmux", "kill-session"]:
            killed.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(m, "tmux_session_exists", lambda name: name == "clone-repo-stuck3")
    monkeypatch.setattr(m.subprocess, "run", fake_run)

    capsys.readouterr()
    rc = _run(monkeypatch, cfg, name="stuck3", project=str(clone), remove=True)
    assert rc == 1
    assert killed == []  # session must survive a failed removal
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["killed"] is False


def test_gc_merged_alone_runs_as_standalone_action(tmp_path, monkeypatch, wt_env):
    """--gc-merged without --prune must actually run the GC sweep instead of
    falling through to the 'Usage: agentwire worktree <name>...' error —
    --help advertises it, so invoking it standalone must behave, not error (#740)."""
    _, clone = _origin_and_clone(tmp_path, default_branch="develop")
    wt_dir = tmp_path / "worktrees"
    cfg = _config(wt_dir)
    assert _run(monkeypatch, cfg, name="finished3", project=str(clone)) == 0

    rc = _run(monkeypatch, cfg, gc_merged=True, project=str(clone))
    assert rc == 0
    assert reg.entries(clone.resolve()) == []
