"""Tests for agentwire/core.py — same-project detection for default created_by (#715).

Spawning a worktree/session in a genuinely separate project should default to
a standalone root; spawning within the caller's own project should still
inherit the caller as parent. See resolve_default_created_by / _same_project.
"""

import subprocess

from agentwire import core


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], capture_output=True, text=True)
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    return repo


class TestSameProject:
    def test_same_repo(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert core._same_project(repo, repo) is True

    def test_linked_worktree_is_same_project_as_main_repo(self, tmp_path):
        # The caller may be running from a linked worktree of the very repo
        # it's spawning into — that's the common "fan out another worktree
        # from within a worktree session" case and must still count as
        # same-project, even though the two paths differ.
        repo = _make_repo(tmp_path)
        wt = tmp_path / "wt"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "side", str(wt)],
            capture_output=True, text=True,
        )
        assert core._same_project(repo, wt) is True

    def test_different_repos_are_different_projects(self, tmp_path):
        repo_a = _make_repo(tmp_path, "a")
        repo_b = _make_repo(tmp_path, "b")
        assert core._same_project(repo_a, repo_b) is False

    def test_non_git_paths_fall_back_to_path_equality(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert core._same_project(a, a) is True
        assert core._same_project(a, b) is False


class TestLiveSessionCwd:
    """_live_session_cwd never guesses from the session name — resolve_default_created_by
    depends on that (a guessed path is unsafe for an identity comparison)."""

    def test_none_when_session_not_live(self, monkeypatch):
        monkeypatch.setattr(core, "tmux_session_exists", lambda s: False)
        assert core._live_session_cwd("gone") is None

    def test_none_when_tmux_query_fails(self, monkeypatch):
        # Session exists per has-session, but display-message errors — still
        # no fallback guess; the caller must treat this as unknown.
        monkeypatch.setattr(core, "tmux_session_exists", lambda s: True)
        monkeypatch.setattr(
            core.subprocess, "run",
            lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})(),
        )
        assert core._live_session_cwd("flaky") is None

    def test_returns_real_cwd_when_live(self, monkeypatch, tmp_path):
        monkeypatch.setattr(core, "tmux_session_exists", lambda s: True)
        monkeypatch.setattr(
            core.subprocess, "run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})(),
        )
        assert core._live_session_cwd("orchestrator") == tmp_path


class TestResolveDefaultCreatedBy:
    def test_no_caller_returns_none(self, tmp_path):
        assert core.resolve_default_created_by(None, tmp_path) is None

    def test_caller_project_unresolvable_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(core, "_live_session_cwd", lambda s: None)
        assert core.resolve_default_created_by("caller", tmp_path) is None

    def test_same_project_inherits_caller(self, monkeypatch, tmp_path):
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(core, "_live_session_cwd", lambda s: repo)
        assert core.resolve_default_created_by("caller", repo) == "caller"

    def test_cross_project_returns_none(self, monkeypatch, tmp_path):
        repo_a = _make_repo(tmp_path, "a")
        repo_b = _make_repo(tmp_path, "b")
        monkeypatch.setattr(core, "_live_session_cwd", lambda s: repo_a)
        assert core.resolve_default_created_by("caller", repo_b) is None

    def test_dead_caller_session_does_not_fall_back_to_name_guessing(self, monkeypatch, tmp_path):
        # Regression: a worktree caller ("myproject-fix-bug", the hyphenated
        # flat naming `agentwire worktree` uses) that's no longer a live tmux
        # session must NOT resolve via _get_session_project_path's
        # session-name-parsing fallback (which only understands "/"-separated
        # project/branch names and would derive the wrong, nonexistent
        # project "myproject-fix-bug" instead of "myproject"). Unknown must
        # stay unknown — no inheritance — rather than risk a wrong guess.
        monkeypatch.setattr(core, "tmux_session_exists", lambda s: False)
        repo = _make_repo(tmp_path)
        assert core.resolve_default_created_by("myproject-fix-bug", repo) is None
