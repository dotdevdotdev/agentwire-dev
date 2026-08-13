"""Unit tests for the git-is-the-source-of-truth worktree layer (#837, #855).

Two bugs, one root cause: worktree creation was implemented in five places
and only one of them registered (#837), and removal derived the worktree path
from a documented convention instead of asking git (#855). These cover the
shared helpers both fixes route through.
"""

import subprocess
from pathlib import Path

import pytest

from agentwire import pane_manager
from agentwire import worktree_registry as reg
from agentwire.worktree import (
    create_and_register_worktree,
    find_git_worktree,
    linked_git_worktrees,
    list_git_worktrees,
    main_worktree,
    register_worktree,
)


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real single-commit git repo, with the registry isolated to tmp_path."""
    monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path / "registry")
    r = tmp_path / "myapp"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "README.md").write_text("hi\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


# --- list/linked/find: reading git's own registry ---

class TestListGitWorktrees:
    def test_main_checkout_is_first_and_carries_its_branch(self, repo):
        entries = list_git_worktrees(repo)
        assert entries[0]["path"].resolve() == repo.resolve()
        assert entries[0]["branch"] == "main"
        assert entries[0]["detached"] is False

    def test_linked_worktrees_exclude_the_main_checkout(self, repo, tmp_path):
        _git(repo, "worktree", "add", "-b", "feat", str(tmp_path / "wt-feat"))
        linked = linked_git_worktrees(repo)
        assert [e["branch"] for e in linked] == ["feat"]

    def test_detached_worktree_reports_no_branch(self, repo, tmp_path):
        _git(repo, "worktree", "add", "--detach", str(tmp_path / "wt-detached"))
        entry = linked_git_worktrees(repo)[0]
        assert entry["detached"] is True
        assert entry["branch"] is None
        assert entry["head"]

    def test_non_repo_returns_nothing_known(self, tmp_path):
        assert list_git_worktrees(tmp_path / "not-a-repo") == []
        assert linked_git_worktrees(tmp_path / "not-a-repo") == []


class TestFindGitWorktree:
    def test_finds_a_worktree_at_a_non_conventional_path(self, repo, tmp_path):
        """#855: neither documented layout — git still knows exactly where it is."""
        odd = tmp_path / "somewhere" / "totally" / "else"
        odd.parent.mkdir(parents=True)
        _git(repo, "worktree", "add", "-b", "odd", str(odd))

        assert find_git_worktree(repo, branch="odd")["path"].resolve() == odd.resolve()
        assert find_git_worktree(repo, name="else")["path"].resolve() == odd.resolve()
        assert find_git_worktree(repo, path=odd)["path"].resolve() == odd.resolve()

    def test_unknown_name_is_none_not_a_guess(self, repo):
        assert find_git_worktree(repo, branch="nope", name="nope") is None

    def test_never_returns_the_main_checkout(self, repo):
        """Resolution must not be able to select the repo's own working copy."""
        assert find_git_worktree(repo, path=repo) is None
        assert find_git_worktree(repo, branch="main") is None
        assert find_git_worktree(repo, name="myapp") is None

    def test_path_match_beats_branch_match(self, repo, tmp_path):
        _git(repo, "worktree", "add", "-b", "a", str(tmp_path / "wt-a"))
        _git(repo, "worktree", "add", "-b", "b", str(tmp_path / "wt-b"))
        found = find_git_worktree(repo, path=tmp_path / "wt-b", branch="a")
        assert found["branch"] == "b"


class TestMainWorktree:
    def test_resolves_the_main_checkout_from_a_linked_worktree(self, repo, tmp_path):
        wt = tmp_path / "wt-linked"
        _git(repo, "worktree", "add", "-b", "linked", str(wt))
        assert main_worktree(wt).resolve() == repo.resolve()

    def test_non_repo_falls_back_to_the_path_given(self, tmp_path):
        stray = tmp_path / "not-a-repo"
        assert main_worktree(stray) == stray


# --- register_worktree: record git's path, key by the main checkout ---

class TestRegisterWorktree:
    def test_records_the_path_git_reports_not_the_one_passed(self, repo, tmp_path):
        """A caller's path may be un-normalized (symlinks, /var vs /private/var);
        later lookups must compare like with like."""
        wt = tmp_path / "wt-norm"
        _git(repo, "worktree", "add", "-b", "norm", str(wt))
        register_worktree(repo, branch="norm", session="myapp-norm", base="main",
                          worktree_path=Path(str(wt) + "/."))

        recorded = Path(reg.entries(repo)[0]["worktree_path"])
        assert recorded == find_git_worktree(repo, branch="norm")["path"]

    def test_keys_the_registry_by_the_main_checkout(self, repo, tmp_path):
        """Passing a LINKED worktree as project_path must not shard the registry."""
        src = tmp_path / "wt-src"
        _git(repo, "worktree", "add", "-b", "src", str(src))
        dst = tmp_path / "wt-dst"
        _git(repo, "worktree", "add", "-b", "dst", str(dst))

        register_worktree(src, branch="dst", session="myapp-dst", base="main",
                          worktree_path=dst)

        assert [e["branch"] for e in reg.entries(repo)] == ["dst"]
        assert reg.entries(src) == []


# --- create_and_register_worktree: the one creation path (#837) ---

class TestCreateAndRegisterWorktree:
    def test_creates_and_registers_in_one_call(self, repo, tmp_path):
        wt = tmp_path / "wt-new"
        ok, err = create_and_register_worktree(
            repo, branch="newbr", worktree_path=wt, session="myapp-newbr",
            base="main", kind="worker",
        )
        assert (ok, err) == (True, "")
        assert wt.exists()
        entry = reg.entries(repo)[0]
        assert entry["branch"] == "newbr"
        assert entry["kind"] == "worker"
        assert entry["topology"] == "worktree"

    def test_adopting_an_existing_worktree_heals_the_registry(self, repo, tmp_path):
        """Re-running creation over an unregistered worktree registers it,
        instead of leaving the orphan #837 is about."""
        wt = tmp_path / "wt-adopt"
        _git(repo, "worktree", "add", "-b", "adopt", str(wt))
        assert reg.entries(repo) == []

        ok, _ = create_and_register_worktree(
            repo, branch="adopt", worktree_path=wt, session="myapp-adopt", base="main",
        )
        assert ok
        assert [e["branch"] for e in reg.entries(repo)] == ["adopt"]

    def test_plain_directory_at_the_path_is_a_hard_failure(self, repo, tmp_path):
        """Never register — or launch an agent into — a directory that merely
        SITS at the worktree path without being one."""
        impostor = tmp_path / "impostor"
        impostor.mkdir()
        ok, err = create_and_register_worktree(
            repo, branch="x", worktree_path=impostor, session="s", base="main",
        )
        assert ok is False
        assert "not a git worktree" in err
        assert reg.entries(repo) == []

    def test_failed_creation_registers_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path / "registry")
        not_a_repo = tmp_path / "nope"
        not_a_repo.mkdir()
        ok, err = create_and_register_worktree(
            not_a_repo, branch="x", worktree_path=tmp_path / "wt",
            session="s", base="main",
        )
        assert ok is False
        assert "Failed to create worktree" in err
        assert reg.entries(not_a_repo) == []


# --- pane_manager: `agentwire spawn --branch` is no longer invisible (#837) ---

class TestWorkerPaneWorktreeRegistration:
    def test_spawn_branch_worktree_is_registered_as_pane_topology(self, repo):
        path = pane_manager.create_worker_worktree("pane-work", str(repo),
                                                   session="myapp")
        assert Path(path).exists()

        entry = reg.entries(repo)[0]
        assert entry["branch"] == "pane-work"
        assert entry["session"] == "myapp"
        assert entry["kind"] == "worker"
        # "pane" — the session recorded is the OWNER, not a session that IS
        # this worktree, so teardown must never kill it.
        assert entry["topology"] == "pane"

    def test_several_panes_in_one_session_all_stay_registered(self, repo):
        """The session name is a shared owner here, so it must NOT be an
        identity key — deduping by it would evict every branch but the newest."""
        pane_manager.create_worker_worktree("pane-a", str(repo), session="myapp")
        pane_manager.create_worker_worktree("pane-b", str(repo), session="myapp")

        assert sorted(e["branch"] for e in reg.entries(repo)) == ["pane-a", "pane-b"]

    def test_outside_a_git_repo_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path / "registry")
        with pytest.raises(RuntimeError, match="Not in a git repository"):
            pane_manager.create_worker_worktree("x", str(tmp_path))


# --- pane-topology entries must never take down their owning session ---

class TestPaneTopologyTeardownGuard:
    def test_remove_does_not_kill_a_pane_entrys_owning_session(self, repo, tmp_path,
                                                               monkeypatch, capsys):
        """A pane entry's session is the ORCHESTRATOR's. Killing it on teardown
        of a worker branch would take down the wrong thing entirely (#837)."""
        from agentwire import session_cli as m

        monkeypatch.setattr(m.chrome_tabs, "REGISTRY_FILE", tmp_path / "tabs.json")
        monkeypatch.setattr(m.shutil, "which", lambda *_: None)
        monkeypatch.setattr(m, "tmux_session_exists", lambda name: name == "myapp")

        killed = []
        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd[:2] == ["tmux", "kill-session"]:
                killed.append(cmd)
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(m.subprocess, "run", fake_run)
        wt = Path(pane_manager.create_worker_worktree("pane-kill", str(repo),
                                                      session="myapp"))

        args = type("A", (), {"name": "pane-kill", "keep_branch": True})()
        rc = m._worktree_remove(args, repo, tmp_path / "worktrees", json_mode=True)

        assert rc == 0
        assert not wt.exists()
        assert killed == []  # the orchestrator session survives


class TestProjectResolvesToMainCheckout:
    """#954 — `worktree --list`/`--prune` run from INSIDE a linked worktree
    read the registry keyed on the worktree's directory name and reported a
    confident all-clear. Every mode must resolve the same registry key
    regardless of which checkout the command runs from."""

    def _make_linked(self, repo, tmp_path):
        wt = tmp_path / "voice-control"
        _git(repo, "worktree", "add", "-q", "-b", "voice-control", str(wt))
        return wt

    def test_list_from_inside_a_worktree_sees_the_registry(self, repo, tmp_path,
                                                           monkeypatch, capsys):
        from agentwire import session_cli as m

        register_worktree(repo, branch="other", session="myapp-other",
                          base="main", worktree_path=tmp_path / "other-wt")
        wt = self._make_linked(repo, tmp_path)
        monkeypatch.setattr(m.os, "getcwd", lambda: str(wt))
        monkeypatch.setattr(m, "tmux_session_exists", lambda *_: False)

        args = type("A", (), {"json": True, "name": None, "list": True,
                              "all": False, "project": None})()
        assert m.cmd_worktree(args) == 0
        import json as _json
        out = _json.loads(capsys.readouterr().out)
        assert [e["session"] for e in out["entries"]] == ["myapp-other"]

    def test_prune_from_inside_a_worktree_prunes_the_stale_entry(self, repo, tmp_path,
                                                                 monkeypatch, capsys):
        from agentwire import session_cli as m

        # A registered path that never exists on disk = the stale-entry shape
        # --prune exists to drop.
        register_worktree(repo, branch="gone", session="myapp-gone",
                          base="main", worktree_path=tmp_path / "long-gone")
        wt = self._make_linked(repo, tmp_path)
        monkeypatch.setattr(m.os, "getcwd", lambda: str(wt))
        monkeypatch.setattr(m, "tmux_session_exists", lambda *_: False)

        args = type("A", (), {"json": True, "name": None, "list": False,
                              "watch": False, "prune": True, "gc_merged": False,
                              "all": False, "project": None})()
        assert m.cmd_worktree(args) == 0
        import json as _json
        out = _json.loads(capsys.readouterr().out)
        assert out["pruned"] == ["myapp-gone"]


class TestTeardownOccupancyGuard:
    """#954 — teardown resolved a session name that matched nothing live and
    removed the worktree anyway, leaving the REAL session running inside a
    deleted directory. When the resolved name is dead but a live session's
    pane cwd is inside the worktree, refuse."""

    def test_refuses_when_a_live_unresolved_session_occupies_the_worktree(
            self, repo, tmp_path, monkeypatch):
        from agentwire import session_cli as m

        wt = tmp_path / "wt-occupied"
        _git(repo, "worktree", "add", "-q", "-b", "occupied", str(wt))
        monkeypatch.setattr(m, "tmux_session_exists", lambda *_: False)
        monkeypatch.setattr(m, "_sessions_by_path",
                            lambda: {str(wt.resolve()): "myapp-occupied"})

        result = m._teardown_entry(repo, tmp_path, "voice-control-occupied", wt,
                                   "occupied", "main")
        assert result["success"] is False
        assert "myapp-occupied" in result["error"]
        assert wt.exists()

    def test_matching_occupant_does_not_trip_the_guard(self, repo, tmp_path, monkeypatch):
        from agentwire import session_cli as m

        wt = tmp_path / "wt-mine"
        _git(repo, "worktree", "add", "-q", "-b", "mine", str(wt))
        monkeypatch.setattr(m.chrome_tabs, "REGISTRY_FILE", tmp_path / "tabs.json")
        monkeypatch.setattr(m.shutil, "which", lambda *_: None)
        monkeypatch.setattr(m, "tmux_session_exists", lambda *_: False)
        monkeypatch.setattr(m, "_sessions_by_path",
                            lambda: {str(wt.resolve()): "myapp-mine"})

        result = m._teardown_entry(repo, tmp_path, "myapp-mine", wt,
                                   "mine", "main", keep_branch=True)
        assert result["success"] is True
        assert not wt.exists()


class TestDurabilityGuard:
    """#941 — session/worktree teardown is authorized by DURABILITY (nothing
    uncommitted at risk), branch deletion by a verified merge. A dirty
    worktree refuses; a clean-but-never-merged one removes fine with the
    branch kept."""

    def _teardown(self, m, repo, tmp_path, wt, **kw):
        return m._teardown_entry(repo, tmp_path, "myapp-x", wt, "x", "main",
                                 keep_branch=True, **kw)

    @pytest.fixture
    def clean_env(self, tmp_path, monkeypatch):
        from agentwire import session_cli as m

        monkeypatch.setattr(m.chrome_tabs, "REGISTRY_FILE", tmp_path / "tabs.json")
        monkeypatch.setattr(m.shutil, "which", lambda *_: None)
        monkeypatch.setattr(m, "tmux_session_exists", lambda *_: False)
        monkeypatch.setattr(m, "_sessions_by_path", lambda: {})
        return m

    def test_dirty_worktree_refuses(self, repo, tmp_path, clean_env):
        m = clean_env
        wt = tmp_path / "wt-dirty"
        _git(repo, "worktree", "add", "-q", "-b", "x", str(wt))
        (wt / "uncommitted.txt").write_text("real work\n")

        result = self._teardown(m, repo, tmp_path, wt)
        assert result["success"] is False
        assert "--discard-changes" in result["error"]
        assert wt.exists()
        assert (wt / "uncommitted.txt").exists()

    def test_discard_changes_overrides(self, repo, tmp_path, clean_env):
        m = clean_env
        wt = tmp_path / "wt-dirty2"
        _git(repo, "worktree", "add", "-q", "-b", "x", str(wt))
        (wt / "uncommitted.txt").write_text("discard me\n")

        result = self._teardown(m, repo, tmp_path, wt, discard_changes=True)
        assert result["success"] is True
        assert not wt.exists()

    def test_clean_never_merged_worktree_removes_and_keeps_the_branch(
            self, repo, tmp_path, clean_env):
        # The #941 spike shape: committed work, a branch that by design never
        # merges. Session/worktree teardown is legitimate; the branch persists.
        m = clean_env
        wt = tmp_path / "wt-spike"
        _git(repo, "worktree", "add", "-q", "-b", "x", str(wt))
        (wt / "spike.txt").write_text("committed\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", "spike work")

        result = self._teardown(m, repo, tmp_path, wt)
        assert result["success"] is True
        assert not wt.exists()
        assert _git(repo, "rev-parse", "--verify", "x").returncode == 0


# --- doctor: orphaned worktrees are finally visible (#837) ---

class TestFindOrphanedWorktrees:
    def _rows(self, path, session, **kw):
        return [{"session": session, "branch": "b", "project": "/p",
                 "worktree_path": str(path), **kw}]

    def test_on_disk_worktree_with_a_dead_session_is_an_orphan(self, tmp_path, monkeypatch):
        from agentwire import doctor_cli

        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda *_: False)
        wt = tmp_path / "left-behind"
        wt.mkdir()
        assert len(doctor_cli.find_orphaned_worktrees(self._rows(wt, "dead"))) == 1

    def test_live_session_is_not_an_orphan(self, tmp_path, monkeypatch):
        from agentwire import doctor_cli

        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda *_: True)
        wt = tmp_path / "working"
        wt.mkdir()
        assert doctor_cli.find_orphaned_worktrees(self._rows(wt, "live")) == []

    def test_stale_entry_with_no_directory_is_prunes_job_not_ours(self, tmp_path, monkeypatch):
        from agentwire import doctor_cli

        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda *_: False)
        gone = tmp_path / "never-existed"
        assert doctor_cli.find_orphaned_worktrees(self._rows(gone, "dead")) == []

    def test_pane_entry_orphans_only_once_its_owner_session_is_gone(self, tmp_path, monkeypatch):
        from agentwire import doctor_cli

        wt = tmp_path / "pane-wt"
        wt.mkdir()
        rows = self._rows(wt, "orchestrator", topology="pane")

        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda *_: True)
        assert doctor_cli.find_orphaned_worktrees(rows) == []

        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda *_: False)
        assert doctor_cli.find_orphaned_worktrees(rows)[0]["topology"] == "pane"


# --- dangling-PR scan skips pane entries (#837) ---

def test_dangling_scan_skips_pane_topology(monkeypatch):
    """A worker pane's parent IS pane 0 of the session on its entry, so the
    liveness gate already proves the parent is live — it can't be dangling."""
    from agentwire import session_cli as m

    monkeypatch.setattr(m.shutil, "which", lambda *_: "/usr/bin/gh")
    monkeypatch.setattr(m, "tmux_session_exists", lambda *_: True)

    def boom(*a, **k):  # gh must never be consulted for a pane entry
        raise AssertionError("pane entry should have been skipped")

    monkeypatch.setattr(m.subprocess, "run", boom)
    rows = [{"session": "orch", "branch": "b", "project": "/p", "topology": "pane"}]
    assert m.scan_dangling_worktrees(rows) == []
