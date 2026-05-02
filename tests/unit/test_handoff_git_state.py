"""Tests for agentwire/handoff/git_state.py."""

import subprocess
from pathlib import Path

import pytest

from agentwire.handoff import git_state


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@test"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["commit", "--allow-empty", "-m", "initial"], repo)
    return repo


@pytest.fixture
def dirty_repo(empty_repo: Path) -> Path:
    (empty_repo / "tracked.txt").write_text("hello\n")
    _git(["add", "tracked.txt"], empty_repo)
    _git(["commit", "-m", "add tracked"], empty_repo)
    # Now make it dirty
    (empty_repo / "tracked.txt").write_text("hello\nworld\n")
    (empty_repo / "untracked.txt").write_text("new\n")
    return empty_repo


class TestIsGitRepo:
    def test_no_repo(self, tmp_path: Path):
        assert git_state.is_git_repo(tmp_path) is False

    def test_inside_repo(self, empty_repo: Path):
        assert git_state.is_git_repo(empty_repo) is True


class TestSnapshot:
    def test_no_repo_returns_blank_state(self, tmp_path: Path):
        snap = git_state.snapshot(tmp_path)
        assert snap["is_repo"] is False
        assert snap["branch"] is None
        assert snap["status"] == ""
        assert snap["diff"] == ""

    def test_clean_repo(self, empty_repo: Path):
        snap = git_state.snapshot(empty_repo)
        assert snap["is_repo"] is True
        assert snap["branch"] in ("main", "master")
        assert snap["commit"] is not None
        assert len(snap["commit"]) == 40
        assert snap["status"] == ""

    def test_dirty_repo_status_and_diff(self, dirty_repo: Path):
        snap = git_state.snapshot(dirty_repo)
        assert snap["is_repo"] is True
        assert "tracked.txt" in snap["status"]
        assert "untracked.txt" in snap["status"]
        # The unstaged modification appears in diff
        assert "world" in snap["diff"]

    def test_log_present(self, dirty_repo: Path):
        snap = git_state.snapshot(dirty_repo)
        assert "add tracked" in snap["log"]


class TestDiffTruncation:
    def test_long_diff_gets_truncated(self, empty_repo: Path):
        # Build a big modification
        path = empty_repo / "big.txt"
        _git(["commit", "--allow-empty", "-m", "marker"], empty_repo)
        path.write_text("line\n" * 5000)
        _git(["add", "big.txt"], empty_repo)
        diff = git_state.diff(empty_repo, staged=True, max_lines=100)
        assert "[... diff truncated at 100 lines ...]" in diff
        assert diff.count("\n") <= 102  # 100 lines + truncation marker + slack
