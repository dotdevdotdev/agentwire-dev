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




class TestSnapshot:


    def test_dirty_repo_status_and_diff(self, dirty_repo: Path):
        snap = git_state.snapshot(dirty_repo)
        assert snap["is_repo"] is True
        assert "tracked.txt" in snap["status"]
        assert "untracked.txt" in snap["status"]
        # The unstaged modification appears in diff
        assert "world" in snap["diff"]



