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



class TestFindGitWorktree:
    def test_finds_a_worktree_at_a_non_conventional_path(self, repo, tmp_path):
        """#855: neither documented layout — git still knows exactly where it is."""
        odd = tmp_path / "somewhere" / "totally" / "else"
        odd.parent.mkdir(parents=True)
        _git(repo, "worktree", "add", "-b", "odd", str(odd))

        assert find_git_worktree(repo, branch="odd")["path"].resolve() == odd.resolve()
        assert find_git_worktree(repo, name="else")["path"].resolve() == odd.resolve()
        assert find_git_worktree(repo, path=odd)["path"].resolve() == odd.resolve()







# --- register_worktree: record git's path, key by the main checkout ---



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





# --- pane_manager: `agentwire spawn --branch` is no longer invisible (#837) ---



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




class TestTeardownRecordRule:
    """#1327 — a record is dropped at teardown only when nothing is left to
    resume into: session owned, worktree removed, branch merged-and-deleted.
    Anything else keeps it so `restart -s` still works."""

    @pytest.fixture
    def env(self, repo, tmp_path, monkeypatch):
        from agentwire import session_cli as m
        from agentwire.core import session_metadata_path, store_session_metadata

        monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
        monkeypatch.setattr(m.chrome_tabs, "REGISTRY_FILE", tmp_path / "tabs.json")
        monkeypatch.setattr(m.shutil, "which", lambda *_: None)
        monkeypatch.setattr(m, "tmux_session_exists", lambda *_: False)
        monkeypatch.setattr(m, "_sessions_by_path", lambda: {})
        # No origin in the fixture repo: merge state is what the tests dictate.
        monkeypatch.setattr(m, "_branch_merge_state", lambda *_: "merged")

        wt = tmp_path / "wt-x"
        _git(repo, "worktree", "add", "-q", "-b", "x", str(wt))
        store_session_metadata("myapp-x", {"cwd_at_launch": str(wt),
                                           "conversation_ids": ["c1"]})
        path = session_metadata_path("myapp-x")
        assert path.exists()
        return m, wt, path

    def _td(self, m, repo, tmp_path, wt, **kw):
        return m._teardown_entry(repo, tmp_path, "myapp-x", wt, "x", "main", **kw)

    def test_merged_and_removed_leaves_no_record(self, env, repo, tmp_path):
        m, wt, path = env
        result = self._td(m, repo, tmp_path, wt)
        assert result["branch_deleted"] and result["record_dropped"]
        assert result["record_left"] is None
        assert not path.exists()

    def test_kept_branch_keeps_the_record(self, env, repo, tmp_path):
        m, wt, path = env
        result = self._td(m, repo, tmp_path, wt, keep_branch=True)
        assert not result["record_dropped"] and result["record_left"]
        assert path.exists()

    def test_unmerged_branch_keeps_the_record(self, env, repo, tmp_path, monkeypatch):
        m, wt, path = env
        monkeypatch.setattr(m, "_branch_merge_state", lambda *_: "open")
        result = self._td(m, repo, tmp_path, wt)
        assert not result["branch_deleted"]
        assert not result["record_dropped"] and path.exists()

    def test_pane_entry_never_drops_its_owners_record(self, env, repo, tmp_path):
        m, wt, path = env
        result = self._td(m, repo, tmp_path, wt, kill_session=False)
        assert not result["record_dropped"] and path.exists()


# --- doctor: orphaned worktrees are finally visible (#837) ---



# --- dangling-PR scan skips pane entries (#837) ---

