"""Tests for agentwire/worktree.py — Session name parsing, paths."""

import subprocess
from pathlib import Path

import pytest

from agentwire.worktree import (
    parse_session_name,
    get_session_path,
    is_git_repo,
    get_project_type,
    ensure_worktree,
)


# --- parse_session_name ---

class TestParseSessionName:
    def test_simple(self):
        assert parse_session_name("myapp") == ("myapp", None, None)

    def test_with_branch(self):
        assert parse_session_name("myapp/feature") == ("myapp", "feature", None)

    def test_with_machine(self):
        assert parse_session_name("myapp@server") == ("myapp", None, "server")

    def test_with_branch_and_machine(self):
        assert parse_session_name("myapp/feature@server") == ("myapp", "feature", "server")

    def test_deep_branch(self):
        # "myapp/feat/sub" — first / splits project from branch
        project, branch, machine = parse_session_name("myapp/feat/sub")
        assert project == "myapp"
        assert branch == "feat/sub"
        assert machine is None


# --- get_session_path ---

class TestGetSessionPath:
    def test_simple_project(self):
        projects = Path("/home/user/projects")
        result = get_session_path("myapp", projects)
        assert result == Path("/home/user/projects/myapp")

    def test_worktree_with_suffix(self):
        projects = Path("/home/user/projects")
        result = get_session_path("myapp/feature", projects)
        assert result == Path("/home/user/projects/myapp-worktrees/feature")

    def test_custom_suffix(self):
        projects = Path("/home/user/projects")
        result = get_session_path("myapp/branch", projects, worktree_suffix="-wt")
        assert result == Path("/home/user/projects/myapp-wt/branch")

    def test_machine_ignored_in_path(self):
        projects = Path("/home/user/projects")
        result = get_session_path("myapp@server", projects)
        assert result == Path("/home/user/projects/myapp")


# --- is_git_repo ---

class TestIsGitRepo:
    def test_with_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert is_git_repo(tmp_path) is True

    def test_without_git_dir(self, tmp_path):
        assert is_git_repo(tmp_path) is False


# --- get_project_type ---

class TestGetProjectType:
    def test_full_with_git(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert get_project_type(tmp_path) == "full"

    def test_scratch_without_git(self, tmp_path):
        assert get_project_type(tmp_path) == "scratch"


# --- ensure_worktree seeding (gitignored files like .env) ---

class TestWorktreeSeeding:
    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*a):
            return subprocess.run(["git", "-C", str(repo), *a],
                                  capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (repo / ".gitignore").write_text("secret.env\n")
        (repo / "README.md").write_text("hi\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        return repo, git

    def test_seeds_listed_file_and_keeps_it_ignored(self, tmp_path):
        repo, git = self._repo(tmp_path)
        (repo / "secret.env").write_text("API_KEY=abc\n")  # gitignored, in main only

        wt = tmp_path / "repo-worktrees" / "feature"
        assert ensure_worktree(repo, "feature", wt, copy_files=["secret.env"])

        # Seeded into the worktree...
        assert (wt / "secret.env").read_text() == "API_KEY=abc\n"
        # ...and still gitignored there, so it's never committed.
        status = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        assert "secret.env" not in status

    def test_only_listed_files_copied(self, tmp_path):
        repo, git = self._repo(tmp_path)
        (repo / "secret.env").write_text("k\n")
        (repo / "other.local").write_text("nope\n")  # untracked, not listed

        wt = tmp_path / "repo-worktrees" / "f2"
        ensure_worktree(repo, "f2", wt, copy_files=["secret.env"])
        assert (wt / "secret.env").exists()
        assert not (wt / "other.local").exists()

    def test_missing_seed_file_is_noop(self, tmp_path):
        repo, _ = self._repo(tmp_path)
        wt = tmp_path / "repo-worktrees" / "f3"
        # Listed file doesn't exist — creation still succeeds.
        assert ensure_worktree(repo, "f3", wt, copy_files=["secret.env"])
        assert not (wt / "secret.env").exists()
