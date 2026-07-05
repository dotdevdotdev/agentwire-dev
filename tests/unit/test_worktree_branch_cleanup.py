"""Unit tests for session_cli's branch-cleanup helpers (#717).

`_branch_merge_state` / `_delete_branch_if_safe` back `worktree --remove`'s
best-effort local+remote branch deletion. gh is stubbed via `shutil.which`
so these stay hermetic (no real gh CLI / network dependency).
"""

import subprocess

from agentwire import session_cli as m


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def _origin_and_clone(tmp_path, default_branch="main"):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", default_branch)
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / "README.md").write_text("hi\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "base")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   capture_output=True, text=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    return origin, clone


class TestBranchMergeState:
    def test_prefers_gh_pr_view_merged(self, tmp_path, monkeypatch):
        _, clone = _origin_and_clone(tmp_path)
        _git(clone, "branch", "feature")
        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="MERGED\n", stderr="")
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(m.shutil, "which", lambda *_: "/usr/bin/gh")
        monkeypatch.setattr(m.subprocess, "run", fake_run)

        assert m._branch_merge_state(clone, "feature", "main") == "merged"

    def test_gh_open_pr_is_not_merged(self, tmp_path, monkeypatch):
        _, clone = _origin_and_clone(tmp_path)
        _git(clone, "branch", "feature")
        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="OPEN\n", stderr="")
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(m.shutil, "which", lambda *_: "/usr/bin/gh")
        monkeypatch.setattr(m.subprocess, "run", fake_run)

        assert m._branch_merge_state(clone, "feature", "main") == "open"

    def test_falls_back_to_git_ancestor_when_gh_unavailable(self, tmp_path, monkeypatch):
        _, clone = _origin_and_clone(tmp_path)
        # Branch identical to base tip -> trivially an ancestor of origin/main.
        _git(clone, "branch", "feature")
        monkeypatch.setattr(m.shutil, "which", lambda *_: None)

        assert m._branch_merge_state(clone, "feature", "main") == "merged"

    def test_unknown_when_diverged_and_no_gh(self, tmp_path, monkeypatch):
        _, clone = _origin_and_clone(tmp_path)
        _git(clone, "checkout", "-q", "-b", "feature")
        (clone / "new.txt").write_text("x\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-qm", "wip")
        _git(clone, "checkout", "-q", "main")
        monkeypatch.setattr(m.shutil, "which", lambda *_: None)

        assert m._branch_merge_state(clone, "feature", "main") == "unknown"


class TestDeleteBranchIfSafe:
    def test_skips_unmerged_without_force(self, tmp_path, monkeypatch):
        _, clone = _origin_and_clone(tmp_path)
        _git(clone, "checkout", "-q", "-b", "feature")
        (clone / "new.txt").write_text("x\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-qm", "wip")
        _git(clone, "checkout", "-q", "main")
        monkeypatch.setattr(m.shutil, "which", lambda *_: None)

        deleted, note = m._delete_branch_if_safe(clone, "feature", "main")
        assert deleted is False
        assert "not confirmed merged" in note
        assert _git(clone, "branch", "--list", "feature").stdout.strip()

    def test_force_deletes_unmerged(self, tmp_path, monkeypatch):
        _, clone = _origin_and_clone(tmp_path)
        _git(clone, "checkout", "-q", "-b", "feature")
        (clone / "new.txt").write_text("x\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-qm", "wip")
        _git(clone, "checkout", "-q", "main")
        monkeypatch.setattr(m.shutil, "which", lambda *_: None)

        deleted, note = m._delete_branch_if_safe(clone, "feature", "main", force=True)
        assert deleted is True
        assert not _git(clone, "branch", "--list", "feature").stdout.strip()

    def test_deletes_merged_branch(self, tmp_path, monkeypatch):
        _, clone = _origin_and_clone(tmp_path)
        _git(clone, "branch", "feature")
        monkeypatch.setattr(m.shutil, "which", lambda *_: None)

        deleted, note = m._delete_branch_if_safe(clone, "feature", "main")
        assert deleted is True
        assert note == "merged"
        assert not _git(clone, "branch", "--list", "feature").stdout.strip()
