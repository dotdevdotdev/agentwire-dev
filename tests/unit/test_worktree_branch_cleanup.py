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


    def test_merged_verdict_requires_matching_head_sha(self, tmp_path, monkeypatch):
        """`gh pr view <branch>` resolves by head-branch NAME, not ref identity —
        a long-merged PR whose remote branch was since deleted can still be the
        match if a LATER branch reuses the same name (agentwire's own worktree
        naming defaults recur: "fix-bug", "cleanup", ...). Trusting MERGED
        without checking headRefOid against the branch's actual tip would
        force-delete real, never-merged work under the reused name (#718 review)."""
        _, clone = _origin_and_clone(tmp_path)
        # A brand-new branch reusing a common name, with real unmerged work.
        _git(clone, "checkout", "-q", "-b", "fix-bug")
        (clone / "new.txt").write_text("brand new, never merged\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-qm", "wip")
        _git(clone, "checkout", "-q", "main")
        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd[:3] == ["gh", "pr", "view"]:
                # A STALE, long-merged PR of the same branch name: headRefOid
                # points at some old commit, not this branch's current tip.
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout='{"state": "MERGED", "headRefOid": "0000000000000000000000000000000000dead"}',
                    stderr="",
                )
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(m.shutil, "which", lambda *_: "/usr/bin/gh")
        monkeypatch.setattr(m.subprocess, "run", fake_run)

        # Namesake PR rejected; the branch has real diverged commits, so the
        # git-ancestor fallback also correctly says "not merged".
        assert m._branch_merge_state(clone, "fix-bug", "main") == "unknown"

        deleted, note = m._delete_branch_if_safe(clone, "fix-bug", "main")
        assert deleted is False
        assert _git(clone, "branch", "--list", "fix-bug").stdout.strip()  # real work preserved




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




class TestForceDeleteOpenPrGuard:
    """#756: `force=True` bypasses the merge-state check, but must NOT bypass
    an OPEN PR — deleting the remote head branch of an open PR silently
    closes it. That's a distinct, more surprising destruction than dropping
    unmerged local work, so it needs its own explicit override
    (`close_pr_branch`)."""

    def _stub_open_pr(self, monkeypatch, number=42):
        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f'{{"state": "OPEN", "number": {number}}}', stderr="",
                )
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(m.shutil, "which", lambda *_: "/usr/bin/gh")
        monkeypatch.setattr(m.subprocess, "run", fake_run)

    def test_force_refuses_when_branch_has_open_pr(self, tmp_path, monkeypatch):
        _, clone = _origin_and_clone(tmp_path)
        _git(clone, "checkout", "-q", "-b", "feature")
        (clone / "new.txt").write_text("x\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-qm", "wip")
        _git(clone, "checkout", "-q", "main")
        self._stub_open_pr(monkeypatch, number=42)

        deleted, note = m._delete_branch_if_safe(clone, "feature", "main", force=True)
        assert deleted is False
        assert "#42" in note
        assert "feature" in note
        assert "--close-pr-branch" in note
        assert _git(clone, "branch", "--list", "feature").stdout.strip()  # branch intact




