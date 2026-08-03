"""Tests for agentwire/project_config.py — resolve_posture, ProjectConfig."""


import os
from pathlib import Path

import pytest
import yaml

from agentwire.project_config import (
    BARE,
    DEFAULT_POSTURE,
    POSTURES,
    ProjectConfig,
    WorktreeOverrides,
    ensure_gitignored,
    find_project_config,
    get_parent_from_config,
    get_voice_from_config,
    load_project_config,
    resolve_posture,
    save_project_config,
)

# --- resolve_posture: the single session axis (#729) ---

class TestResolvePosture:
    @pytest.mark.parametrize("value,expected", [
        ("bypass", "bypass"),
        ("prompted", "prompted"),
        ("auto", "auto"),
        ("bare", "bare"),             # the no-agent sentinel
    ])
    def test_valid(self, value, expected):
        assert resolve_posture(value) == expected

    def test_case_normalized(self):
        assert resolve_posture("BYPASS") == "bypass"
        assert resolve_posture("Auto") == "auto"

    def test_defaults_to_bypass(self):
        assert resolve_posture("") == DEFAULT_POSTURE == "bypass"
        assert resolve_posture(None) == "bypass"

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            resolve_posture("nonsense")

    def test_dropped_postures_raise(self):
        # restricted/readonly were removed (#729) — no longer valid
        with pytest.raises(ValueError):
            resolve_posture("restricted")
        with pytest.raises(ValueError):
            resolve_posture("readonly")

    def test_posture_set(self):
        assert POSTURES == ("bypass", "prompted", "auto")

    def test_bare_is_not_a_posture(self):
        # bare is orthogonal — a sentinel, not one of the permission modes
        assert BARE not in POSTURES


# --- ProjectConfig ---

class TestProjectConfig:
    def test_from_dict_full(self):
        data = {
            "posture": "bypass",
            "roles": ["agentwire", "voice"],
            "voice": "default",
            "parent": "main",
        }
        config = ProjectConfig.from_dict(data)
        assert config.posture == "bypass"
        assert config.roles == ["agentwire", "voice"]
        assert config.voice == "default"
        assert config.parent == "main"

    def test_from_dict_defaults(self):
        config = ProjectConfig.from_dict({})
        assert config.posture == "bypass"
        assert config.roles == []
        assert config.voice is None
        assert config.parent is None

    def test_from_dict_dropped_posture_falls_back(self):
        # restricted/readonly are gone → fall back to default, never crash
        assert ProjectConfig.from_dict({"posture": "restricted"}).posture == "bypass"
        assert ProjectConfig.from_dict({"posture": "readonly"}).posture == "bypass"

    def test_from_dict_bad_posture_falls_back(self):
        # Unknown value → default, never a crash on config load
        assert ProjectConfig.from_dict({"posture": "nonsense"}).posture == "bypass"

    def test_roles_string_to_list_coercion(self):
        config = ProjectConfig.from_dict({"roles": "agentwire"})
        assert config.roles == ["agentwire"]

    def test_roles_none_to_empty_list(self):
        config = ProjectConfig.from_dict({"roles": None})
        assert config.roles == []

    def test_to_dict_omits_unset_includes_set(self):
        # Unset optional fields stay out of the dict
        bare = ProjectConfig(posture="bypass").to_dict()
        assert bare == {"posture": "bypass"}
        assert {"voice", "parent", "roles"}.isdisjoint(bare.keys())
        # Populated fields appear with their value
        full = ProjectConfig(
            posture="auto",
            roles=["agentwire"],
            voice="default",
        ).to_dict()
        assert full["posture"] == "auto"
        assert full["roles"] == ["agentwire"]
        assert full["voice"] == "default"

    def test_round_trip(self):
        original = ProjectConfig(
            posture="prompted",
            roles=["voice", "worker"],
            voice="may",
            parent="main",
        )
        d = original.to_dict()
        restored = ProjectConfig.from_dict(d)
        assert restored.posture == original.posture
        assert restored.roles == original.roles
        assert restored.voice == original.voice
        assert restored.parent == original.parent


# --- load/save/find_project_config ---

class TestProjectConfigIO:
    def test_load_from_directory(self, project_dir, project_config_file):
        config = load_project_config(project_dir)
        assert config is not None
        assert config.posture == "bypass"
        assert "agentwire" in config.roles

    def test_load_from_file_path(self, project_config_file):
        config = load_project_config(project_config_file)
        assert config is not None
        assert config.posture == "bypass"

    def test_load_missing_returns_none(self, tmp_path):
        config = load_project_config(tmp_path / "nonexistent")
        assert config is None

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

    def test_find_walks_up_parents(self, tmp_path):
        # Create config in parent
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src" / "deep"
        child.mkdir(parents=True)

        config_path = parent / ".agentwire.yml"
        with open(config_path, "w") as f:
            yaml.safe_dump({"posture": "bare"}, f)

        found = find_project_config(child)
        assert found is not None
        assert found == config_path

    def test_find_returns_none_when_absent(self, tmp_path):
        found = find_project_config(tmp_path)
        assert found is None

    def test_find_falls_back_to_example(self, tmp_path):
        # Only the committed template exists → use it (#620).
        example = tmp_path / ".agentwire.yml.example"
        with open(example, "w") as f:
            yaml.safe_dump({"posture": "bypass", "roles": ["contributor"]}, f)

        found = find_project_config(tmp_path)
        assert found == example

    def test_find_live_wins_over_example(self, tmp_path):
        # A local .agentwire.yml overrides the committed .example at the same level.
        live = tmp_path / ".agentwire.yml"
        example = tmp_path / ".agentwire.yml.example"
        with open(live, "w") as f:
            yaml.safe_dump({"posture": "bare"}, f)
        with open(example, "w") as f:
            yaml.safe_dump({"posture": "bypass", "roles": ["contributor"]}, f)

        found = find_project_config(tmp_path)
        assert found == live

    def test_load_from_directory_uses_example(self, tmp_path):
        with open(tmp_path / ".agentwire.yml.example", "w") as f:
            yaml.safe_dump({"posture": "bypass", "roles": ["contributor"]}, f)

        config = load_project_config(tmp_path)
        assert config is not None
        assert config.posture == "bypass"
        assert config.roles == ["contributor"]


# --- worktree: block — per-project overrides for `agentwire worktree` (#705) ---

class TestWorktreeOverrides:
    def test_full_block(self):
        config = ProjectConfig.from_dict({
            "worktree": {"dir": "/tmp/my-trees", "base": "develop"},
        })
        assert config.worktree.dir == Path("/tmp/my-trees")
        assert config.worktree.base == "develop"

    def test_dir_tilde_expanded(self):
        config = ProjectConfig.from_dict({"worktree": {"dir": "~/work-trees"}})
        assert config.worktree.dir == Path.home() / "work-trees"
        assert config.worktree.base is None

    def test_base_only(self):
        config = ProjectConfig.from_dict({"worktree": {"base": "develop"}})
        assert config.worktree.dir is None
        assert config.worktree.base == "develop"

    def test_absent_block_defaults_empty(self):
        config = ProjectConfig.from_dict({})
        assert config.worktree == WorktreeOverrides()
        assert config.worktree.dir is None
        assert config.worktree.base is None

    def test_unknown_keys_warn_but_parse(self, capsys):
        config = ProjectConfig.from_dict({
            "worktree": {"dir": "/tmp/t", "bogus": 1, "naming": "x"},
        })
        assert config.worktree.dir == Path("/tmp/t")  # known keys still land
        err = capsys.readouterr().err
        assert "bogus" in err and "naming" in err

    def test_non_mapping_block_ignored_with_warning(self, capsys):
        config = ProjectConfig.from_dict({"worktree": "develop"})
        assert config.worktree == WorktreeOverrides()
        assert "worktree" in capsys.readouterr().err

    def test_null_values_treated_as_unset(self):
        config = ProjectConfig.from_dict({"worktree": {"dir": None, "base": None}})
        assert config.worktree.dir is None
        assert config.worktree.base is None

    def test_to_dict_round_trip(self):
        original = ProjectConfig.from_dict({
            "posture": "bypass",
            "worktree": {"dir": "/tmp/my-trees", "base": "develop"},
        })
        restored = ProjectConfig.from_dict(original.to_dict())
        assert restored.worktree == original.worktree

    def test_to_dict_omits_empty_block(self):
        assert "worktree" not in ProjectConfig().to_dict()


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

    def test_to_dict_never_emits_safety(self):
        config = ProjectConfig(posture="bypass")
        assert "safety" not in config.to_dict()


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

    def test_to_dict_never_emits_shell_or_tasks(self):
        config = ProjectConfig(posture="bypass")
        d = config.to_dict()
        assert "shell" not in d
        assert "tasks" not in d


# --- ensure_gitignored ---

def _git(repo, *args):
    import subprocess
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )


class TestEnsureGitignored:
    def test_non_git_dir_is_noop(self, tmp_path):
        assert ensure_gitignored(tmp_path) is False
        assert not (tmp_path / ".gitignore").exists()

    def test_adds_entry_in_git_repo(self, tmp_path):
        _git(tmp_path, "init")
        assert ensure_gitignored(tmp_path) is True
        assert ".agentwire.yml" in (tmp_path / ".gitignore").read_text()

    def test_idempotent_when_already_ignored(self, tmp_path):
        _git(tmp_path, "init")
        assert ensure_gitignored(tmp_path) is True
        before = (tmp_path / ".gitignore").read_text()
        assert ensure_gitignored(tmp_path) is False
        assert (tmp_path / ".gitignore").read_text() == before

    def test_respects_tracked_file(self, tmp_path):
        _git(tmp_path, "init")
        (tmp_path / ".agentwire.yml").write_text("posture: bypass\n")
        _git(tmp_path, "add", ".agentwire.yml")
        _git(tmp_path, "commit", "-m", "track config")
        assert ensure_gitignored(tmp_path) is False
        assert not (tmp_path / ".gitignore").exists()

    def test_appends_after_missing_trailing_newline(self, tmp_path):
        _git(tmp_path, "init")
        (tmp_path / ".gitignore").write_text("*.log")  # no trailing newline
        assert ensure_gitignored(tmp_path) is True
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert "*.log" in lines
        assert ".agentwire.yml" in lines

    def test_save_project_config_gitignores(self, tmp_path):
        _git(tmp_path, "init")
        config = ProjectConfig(posture="bypass")
        assert save_project_config(config, tmp_path) is True
        assert ".agentwire.yml" in (tmp_path / ".gitignore").read_text()

    def test_custom_filename_and_pattern(self, tmp_path):
        """`tasks_cli.py` reuses this for `.agentwire.tasks.yml` w/ a glob pattern."""
        _git(tmp_path, "init")
        assert ensure_gitignored(tmp_path, ".agentwire.tasks.yml", ".agentwire.tasks*.yml") is True
        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".agentwire.tasks*.yml" in gitignore
        assert ".agentwire.yml" not in gitignore


# --- deleted cwd (#850) ---
#
# These tests really chdir into a directory and really delete it, because the
# bug only exists in that state: a guard can look correct and a test can pass
# green while never once putting the process somewhere that no longer exists.


@pytest.fixture
def dead_cwd(tmp_path, monkeypatch):
    """Put the process in a directory, then delete it under its own feet.

    Mirrors a worktree being torn down while a session is still attached to it.
    ``$PWD`` is unset by default so the fallback can't quietly rescue us —
    tests that want the fallback set it back themselves.
    """
    doomed = tmp_path / "worktree-about-to-die"
    doomed.mkdir()
    original = os.getcwd()
    os.chdir(doomed)
    doomed.rmdir()
    monkeypatch.delenv("PWD", raising=False)

    # Precondition: the process really is somewhere that no longer exists.
    with pytest.raises(FileNotFoundError):
        Path.cwd()

    try:
        yield doomed
    finally:
        os.chdir(original)


class TestDeletedCwd:
    def test_find_project_config_degrades_instead_of_raising(self, dead_cwd):
        """The reported crash: zero-arg call with a cwd that's gone (#850)."""
        assert find_project_config() is None

    def test_get_voice_from_config_returns_none(self, dead_cwd):
        """The observed symptom — `agentwire say` dying on the voice lookup.

        Returning None is what lets the caller's `or` chain reach the global
        default voice; raising is what stopped it.
        """
        assert get_voice_from_config() is None

    def test_get_parent_from_config_returns_none(self, dead_cwd):
        assert get_parent_from_config() is None

    def test_load_project_config_returns_none(self, dead_cwd):
        assert load_project_config() is None

    def test_relative_start_path_degrades(self, dead_cwd):
        """`.resolve()` needs the cwd to anchor a relative path, so it raises too."""
        assert find_project_config(Path("some/relative/dir")) is None

    def test_absolute_paths_still_work(self, dead_cwd, tmp_path):
        """Degrading gracefully must not mean degrading always.

        An absolute path never needs the cwd, so a dead cwd is irrelevant to it.
        """
        project = tmp_path / "live-project"
        project.mkdir()
        (project / ".agentwire.yml").write_text("posture: bare\nvoice: echo\n")

        assert find_project_config(project) == project / ".agentwire.yml"
        assert get_voice_from_config(project) == "echo"

    def test_falls_back_to_pwd_env(self, dead_cwd, tmp_path, monkeypatch):
        """$PWD survives the deletion, so a live $PWD still finds the config."""
        project = tmp_path / "still-here"
        project.mkdir()
        (project / ".agentwire.yml").write_text("posture: bare\nvoice: shimmer\n")
        monkeypatch.setenv("PWD", str(project))

        assert find_project_config() == project / ".agentwire.yml"
        assert get_voice_from_config() == "shimmer"

    def test_stale_pwd_env_does_not_rescue(self, dead_cwd, monkeypatch):
        """A $PWD pointing at the same deleted dir degrades, it doesn't raise."""
        monkeypatch.setenv("PWD", str(dead_cwd))
        assert find_project_config() is None

    def test_save_project_config_returns_false_for_relative(self, dead_cwd):
        """Unwritable, but by returning False — its documented failure mode."""
        assert save_project_config(ProjectConfig(posture="bypass"), Path("rel/dir")) is False

    def test_save_project_config_still_works_for_absolute(self, dead_cwd, tmp_path):
        target = tmp_path / "writable"
        target.mkdir()
        assert save_project_config(ProjectConfig(posture="bare"), target) is True
        assert (target / ".agentwire.yml").exists()

    def test_ensure_gitignored_returns_false_for_relative(self, dead_cwd):
        assert ensure_gitignored(Path("rel/dir")) is False

    def test_custom_filename_idempotent_via_glob(self, tmp_path):
        _git(tmp_path, "init")
        ensure_gitignored(tmp_path, ".agentwire.tasks.yml", ".agentwire.tasks*.yml")
        before = (tmp_path / ".gitignore").read_text()
        # The proposed staging file is already covered by the glob line above.
        assert ensure_gitignored(tmp_path, ".agentwire.tasks.proposed.yml", ".agentwire.tasks*.yml") is False
        assert (tmp_path / ".gitignore").read_text() == before
