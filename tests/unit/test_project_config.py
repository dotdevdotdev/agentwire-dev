"""Tests for agentwire/project_config.py — resolve_posture, ProjectConfig."""




from agentwire.project_config import (
    ProjectConfig,
    ensure_gitignored,
    load_project_config,
    save_project_config,
)

# --- resolve_posture: the single session axis (#729) ---

# --- ProjectConfig ---



# --- load/save/find_project_config ---

class TestProjectConfigIO:



    def test_save_and_reload(self, project_dir):
        config = ProjectConfig(
            posture="prompted",
            roles=["voice"],
            voice="echo",
        )
        assert save_project_config(config, project_dir) is True

        loaded = load_project_config(project_dir)
        assert loaded is not None
        assert loaded.posture == "prompted"
        assert loaded.roles == ["voice"]
        assert loaded.voice == "echo"







# --- worktree: block — per-project overrides for `agentwire worktree` (#705) ---



# --- ProjectConfig holds no safety config (#466/#467) ---

class TestProjectConfigNoSafety:
    def test_safety_block_is_ignored(self):
        """A `safety:` block in .agentwire.yml is no longer parsed into the config.

        Per-project safety policy (incl. allowed_paths) lives in the protected
        .damagecontrol.yml — .agentwire.yml carries none.
        """
        config = ProjectConfig.from_dict({
            "posture": "bypass",
            "safety": {"allowed_paths": [{"path": "dist/*", "allow": "all"}]},
        })
        assert not hasattr(config, "safety")



# --- ProjectConfig holds no task-execution config either (#720) ---

class TestProjectConfigNoTasks:
    def test_shell_and_tasks_are_ignored(self):
        """`shell:`/`tasks:` in .agentwire.yml are no longer parsed into the config.

        Task-execution config (pre/post/on_task_end/shell) lives in the
        protected .agentwire.tasks.yml — .agentwire.yml carries none of it.
        """
        config = ProjectConfig.from_dict({
            "posture": "bypass",
            "shell": "/bin/bash",
            "tasks": {"t1": {"prompt": "hello"}},
        })
        assert not hasattr(config, "shell")
        assert not hasattr(config, "tasks")



# --- ensure_gitignored ---

def _git(repo, *args):
    import subprocess
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )


class TestEnsureGitignored:

    def test_adds_entry_in_git_repo(self, tmp_path):
        _git(tmp_path, "init")
        assert ensure_gitignored(tmp_path) is True
        assert ".agentwire.yml" in (tmp_path / ".gitignore").read_text()







# --- deleted cwd (#850) ---
#
# These tests really chdir into a directory and really delete it, because the
# bug only exists in that state: a guard can look correct and a test can pass
# green while never once putting the process somewhere that no longer exists.




