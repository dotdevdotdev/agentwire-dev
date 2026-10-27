"""Tests for agentwire/worktree.py — Session name parsing, paths."""

import subprocess


from agentwire.worktree import (
    ensure_worktree,
    is_registered_worktree,
    remove_worktree,
)


def _make_repo(tmp_path, name="repo", default_branch="main"):
    """Init a git repo with one commit on `default_branch`."""
    repo = tmp_path / name
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True)

    git("init", "-q", "-b", default_branch)
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return repo, git


# --- parse_session_name ---



# --- is_git_repo ---



# --- get_project_type ---



# --- slugify / apply_naming ---



# --- is_valid_branch_name ---



# --- git_root ---



# --- git_common_dir (the "same repo" signal that survives linked worktrees) ---



# --- default_base_branch (repo-derived, no hardcoded main) ---



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




    def test_mandatory_files_seeded_even_when_copy_files_omits_them(self, tmp_path):
        # #803: a config.yaml written before the #720 task-file split can
        # carry a stale `copy_files: [.env, .agentwire.yml]` that predates
        # `.agentwire.tasks.yml` — every worktree-dispatched scheduled task
        # then fails with "No .agentwire.tasks.yml found". These three are
        # mandatory regardless of what the configured list says.
        repo, git = self._repo(tmp_path)
        (repo / ".env").write_text("API_KEY=abc\n")
        (repo / ".agentwire.yml").write_text("posture: bypass\n")
        (repo / ".agentwire.tasks.yml").write_text("tasks:\n  x:\n    prompt: hi\n")

        wt = tmp_path / "repo-worktrees" / "f4"
        # Stale/narrow copy_files — omits .agentwire.tasks.yml entirely.
        assert ensure_worktree(repo, "f4", wt, copy_files=[".env", ".agentwire.yml"])

        assert (wt / ".env").read_text() == "API_KEY=abc\n"
        assert (wt / ".agentwire.yml").read_text() == "posture: bypass\n"
        assert (wt / ".agentwire.tasks.yml").exists()



# --- remove_worktree (#717: force by default, reports (removed, error)) ---

class TestRemoveWorktree:
    def test_force_removes_dirty_worktree(self, tmp_path):
        """Uncommitted changes in the worktree must not block teardown."""
        repo, git = _make_repo(tmp_path)
        wt = tmp_path / "repo-worktrees" / "dirty"
        assert ensure_worktree(repo, "dirty", wt)
        (wt / "scratch.txt").write_text("uncommitted\n")

        removed, error = remove_worktree(repo, wt)
        assert removed is True
        assert error == ""
        assert not wt.exists()




# --- is_registered_worktree ---

class TestIsRegisteredWorktree:

    def test_false_once_gits_own_admin_dir_is_gone(self, tmp_path):
        """A directory can outlive its registration (e.g. a prior teardown's
        `rm` half crashed, or the admin dir was pruned independently) — once
        git's `.git/worktrees/<name>` entry is gone, git itself no longer
        considers this path a worktree, even though it still exists on disk.
        """
        import shutil

        repo, _ = _make_repo(tmp_path)
        wt = tmp_path / "repo-worktrees" / "feature"
        assert ensure_worktree(repo, "feature", wt)

        shutil.rmtree(repo / ".git" / "worktrees" / "feature")

        assert wt.exists()  # leftover directory content untouched
        assert is_registered_worktree(repo, wt) is False


