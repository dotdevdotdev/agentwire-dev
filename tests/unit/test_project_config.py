"""Tests for agentwire/project_config.py — SessionType, ProjectConfig, normalize."""


from pathlib import Path

import pytest
import yaml

from agentwire.project_config import (
    ProjectConfig,
    SessionType,
    WorktreeOverrides,
    compose_session_type,
    ensure_gitignored,
    find_project_config,
    load_project_config,
    normalize_session_type,
    save_project_config,
)

# --- SessionType.from_str ---

class TestSessionTypeFromStr:
    @pytest.mark.parametrize("input_val,expected", [
        ("bare", SessionType.BARE),
        ("claude-bypass", SessionType.CLAUDE_BYPASS),
        ("claude-prompted", SessionType.CLAUDE_PROMPTED),
        ("claude-restricted", SessionType.CLAUDE_RESTRICTED),
        ("standard", SessionType.STANDARD),
        ("worker", SessionType.WORKER),
        ("voice", SessionType.VOICE),
    ])
    def test_valid_types(self, input_val, expected):
        assert SessionType.from_str(input_val) == expected

    def test_case_insensitive(self):
        assert SessionType.from_str("CLAUDE-BYPASS") == SessionType.CLAUDE_BYPASS
        assert SessionType.from_str("Bare") == SessionType.BARE

    def test_underscore_to_hyphen(self):
        assert SessionType.from_str("claude_bypass") == SessionType.CLAUDE_BYPASS
        assert SessionType.from_str("CLAUDE_RESTRICTED") == SessionType.CLAUDE_RESTRICTED

    def test_unknown_defaults_to_standard(self):
        assert SessionType.from_str("nonexistent") == SessionType.STANDARD
        assert SessionType.from_str("") == SessionType.STANDARD

    def test_session_type_is_a_closed_claude_set(self):
        """SessionType is a CLOSED claude-* set — an unknown non-claude type
        (e.g. any former external-harness type) must NOT round-trip; it
        defaults to STANDARD rather than becoming a live type of its own."""
        assert SessionType.from_str("someagent-x") == SessionType.STANDARD
        assert SessionType.from_str("someagent-x-restricted") == SessionType.STANDARD
        assert SessionType.from_str("other-backend") == SessionType.STANDARD


# --- SessionType.to_cli_flags ---

class TestSessionTypeToCliFlags:
    def test_bare_empty(self):
        assert SessionType.BARE.to_cli_flags() == []

    def test_bypass_has_skip_permissions(self):
        flags = SessionType.CLAUDE_BYPASS.to_cli_flags()
        assert "--dangerously-skip-permissions" in flags

    def test_prompted_no_flags(self):
        assert SessionType.CLAUDE_PROMPTED.to_cli_flags() == []

    def test_restricted_has_tools_bash(self):
        flags = SessionType.CLAUDE_RESTRICTED.to_cli_flags()
        assert flags == ["--tools", "Bash"]

    def test_standard_empty(self):
        # Universal types return empty (they need normalizing first)
        assert SessionType.STANDARD.to_cli_flags() == []


# --- normalize_session_type ---

class TestNormalizeSessionType:
    @pytest.mark.parametrize("universal,agent,expected", [
        ("standard", "claude", "claude-bypass"),
        ("worker", "claude", "claude-restricted"),
        ("voice", "claude", "claude-prompted"),
    ])
    def test_universal_mappings(self, universal, agent, expected):
        assert normalize_session_type(universal, agent) == expected

    @pytest.mark.parametrize("agent_specific", [
        "claude-bypass", "claude-prompted", "claude-restricted",
        "bare",
    ])
    def test_agent_specific_passthrough(self, agent_specific):
        assert normalize_session_type(agent_specific, "claude") == agent_specific

    def test_unknown_defaults_to_bypass(self):
        assert normalize_session_type("foobar", "claude") == "claude-bypass"


# --- compose_session_type: the posture axis (#309, collapsed to claude-only #730) ---

class TestComposeSessionType:
    @pytest.mark.parametrize("posture,expected", [
        ("bypass", "claude-bypass"),
        ("prompted", "claude-prompted"),
        ("restricted", "claude-restricted"),
        ("readonly", "claude-restricted"),   # claude's most-locked tier
    ])
    def test_compositions(self, posture, expected):
        assert compose_session_type(posture) == expected

    def test_defaults(self):
        assert compose_session_type("") == "claude-bypass"

    def test_unknown_posture_raises(self):
        with pytest.raises(ValueError):
            compose_session_type("nonsense")


# --- ProjectConfig ---

class TestProjectConfig:
    def test_from_dict_full(self):
        data = {
            "type": "claude-bypass",
            "roles": ["agentwire", "voice"],
            "voice": "default",
            "parent": "main",
        }
        config = ProjectConfig.from_dict(data)
        assert config.type == SessionType.CLAUDE_BYPASS
        assert config.roles == ["agentwire", "voice"]
        assert config.voice == "default"
        assert config.parent == "main"

    def test_from_dict_defaults(self):
        config = ProjectConfig.from_dict({})
        assert config.type == SessionType.STANDARD
        assert config.roles == []
        assert config.voice is None
        assert config.parent is None

    def test_roles_string_to_list_coercion(self):
        config = ProjectConfig.from_dict({"roles": "agentwire"})
        assert config.roles == ["agentwire"]

    def test_roles_none_to_empty_list(self):
        config = ProjectConfig.from_dict({"roles": None})
        assert config.roles == []

    def test_to_dict_omits_unset_includes_set(self):
        # Unset optional fields stay out of the dict
        bare = ProjectConfig(type=SessionType.CLAUDE_BYPASS).to_dict()
        assert bare == {"type": "claude-bypass"}
        assert {"voice", "parent", "roles"}.isdisjoint(bare.keys())
        # Populated fields appear with their value
        full = ProjectConfig(
            type=SessionType.WORKER,
            roles=["agentwire"],
            voice="default",
        ).to_dict()
        assert full["type"] == "worker"
        assert full["roles"] == ["agentwire"]
        assert full["voice"] == "default"

    def test_round_trip(self):
        original = ProjectConfig(
            type=SessionType.CLAUDE_PROMPTED,
            roles=["voice", "worker"],
            voice="may",
            parent="main",
        )
        d = original.to_dict()
        restored = ProjectConfig.from_dict(d)
        assert restored.type == original.type
        assert restored.roles == original.roles
        assert restored.voice == original.voice
        assert restored.parent == original.parent


# --- load/save/find_project_config ---

class TestProjectConfigIO:
    def test_load_from_directory(self, project_dir, project_config_file):
        config = load_project_config(project_dir)
        assert config is not None
        assert config.type == SessionType.CLAUDE_BYPASS
        assert "agentwire" in config.roles

    def test_load_from_file_path(self, project_config_file):
        config = load_project_config(project_config_file)
        assert config is not None
        assert config.type == SessionType.CLAUDE_BYPASS

    def test_load_missing_returns_none(self, tmp_path):
        config = load_project_config(tmp_path / "nonexistent")
        assert config is None

    def test_save_and_reload(self, project_dir):
        config = ProjectConfig(
            type=SessionType.VOICE,
            roles=["voice"],
            voice="echo",
        )
        assert save_project_config(config, project_dir) is True

        loaded = load_project_config(project_dir)
        assert loaded is not None
        assert loaded.type == SessionType.VOICE
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
            yaml.safe_dump({"type": "bare"}, f)

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
            yaml.safe_dump({"type": "claude-bypass", "roles": ["contributor"]}, f)

        found = find_project_config(tmp_path)
        assert found == example

    def test_find_live_wins_over_example(self, tmp_path):
        # A local .agentwire.yml overrides the committed .example at the same level.
        live = tmp_path / ".agentwire.yml"
        example = tmp_path / ".agentwire.yml.example"
        with open(live, "w") as f:
            yaml.safe_dump({"type": "bare"}, f)
        with open(example, "w") as f:
            yaml.safe_dump({"type": "claude-bypass", "roles": ["contributor"]}, f)

        found = find_project_config(tmp_path)
        assert found == live

    def test_load_from_directory_uses_example(self, tmp_path):
        with open(tmp_path / ".agentwire.yml.example", "w") as f:
            yaml.safe_dump({"type": "claude-bypass", "roles": ["contributor"]}, f)

        config = load_project_config(tmp_path)
        assert config is not None
        assert config.type == SessionType.CLAUDE_BYPASS
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
            "type": "claude-bypass",
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
            "type": "claude-bypass",
            "safety": {"allowed_paths": [{"path": "dist/*", "allow": "all"}]},
        })
        assert not hasattr(config, "safety")

    def test_to_dict_never_emits_safety(self):
        config = ProjectConfig(type=SessionType.CLAUDE_BYPASS)
        assert "safety" not in config.to_dict()


# --- ProjectConfig holds no task-execution config either (#720) ---

class TestProjectConfigNoTasks:
    def test_shell_and_tasks_are_ignored(self):
        """`shell:`/`tasks:` in .agentwire.yml are no longer parsed into the config.

        Task-execution config (pre/post/on_task_end/shell) lives in the
        protected .agentwire.tasks.yml — .agentwire.yml carries none of it.
        """
        config = ProjectConfig.from_dict({
            "type": "claude-bypass",
            "shell": "/bin/bash",
            "tasks": {"t1": {"prompt": "hello"}},
        })
        assert not hasattr(config, "shell")
        assert not hasattr(config, "tasks")

    def test_to_dict_never_emits_shell_or_tasks(self):
        config = ProjectConfig(type=SessionType.CLAUDE_BYPASS)
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
        (tmp_path / ".agentwire.yml").write_text("type: claude-bypass\n")
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
        config = ProjectConfig(type=SessionType.CLAUDE_BYPASS)
        assert save_project_config(config, tmp_path) is True
        assert ".agentwire.yml" in (tmp_path / ".gitignore").read_text()

    def test_custom_filename_and_pattern(self, tmp_path):
        """`tasks_cli.py` reuses this for `.agentwire.tasks.yml` w/ a glob pattern."""
        _git(tmp_path, "init")
        assert ensure_gitignored(tmp_path, ".agentwire.tasks.yml", ".agentwire.tasks*.yml") is True
        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".agentwire.tasks*.yml" in gitignore
        assert ".agentwire.yml" not in gitignore

    def test_custom_filename_idempotent_via_glob(self, tmp_path):
        _git(tmp_path, "init")
        ensure_gitignored(tmp_path, ".agentwire.tasks.yml", ".agentwire.tasks*.yml")
        before = (tmp_path / ".gitignore").read_text()
        # The proposed staging file is already covered by the glob line above.
        assert ensure_gitignored(tmp_path, ".agentwire.tasks.proposed.yml", ".agentwire.tasks*.yml") is False
        assert (tmp_path / ".gitignore").read_text() == before
