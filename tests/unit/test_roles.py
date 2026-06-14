"""Tests for agentwire/roles/__init__.py — Role parsing, merging, discovery."""

from pathlib import Path

import pytest

from agentwire.roles import (
    INTRINSIC_ETIQUETTE,
    RoleConfig,
    MergedRole,
    parse_role_file,
    merge_roles,
    discover_role,
    inject_soul,
    load_roles,
    resolve_roles,
)


@pytest.fixture
def role_file(tmp_path):
    """Create a test role markdown file."""
    path = tmp_path / "test-role.md"
    path.write_text(
        "---\n"
        "name: test-role\n"
        "description: A test role\n"
        "tools: Bash,Read,Write\n"
        "disallowedTools: AskUserQuestion\n"
        'color: "#FF0000"\n'
        "---\n"
        "\n"
        "# Test Role\n"
        "\n"
        "You are a test role.\n"
    )
    return path


# --- parse_role_file ---

class TestParseRoleFile:
    def test_full_frontmatter(self, role_file):
        role = parse_role_file(role_file)
        assert role is not None
        assert role.name == "test-role"
        assert role.description == "A test role"
        assert role.tools == ["Bash", "Read", "Write"]
        assert role.disallowed_tools == ["AskUserQuestion"]
        assert role.color == "#FF0000"
        assert "You are a test role." in role.instructions

    def test_no_frontmatter(self, tmp_path):
        path = tmp_path / "plain.md"
        path.write_text("# Just instructions\n\nDo things.\n")
        role = parse_role_file(path)
        assert role is not None
        assert role.name == "plain"  # Uses stem
        assert role.tools == []
        assert role.disallowed_tools == []

    def test_missing_file(self, tmp_path):
        role = parse_role_file(tmp_path / "nonexistent.md")
        assert role is None

    def test_tools_as_string(self, tmp_path):
        path = tmp_path / "r.md"
        path.write_text("---\nname: r\ntools: Bash,Read\n---\n\nHello\n")
        role = parse_role_file(path)
        assert role is not None
        assert role.tools == ["Bash", "Read"]

    def test_tools_as_list(self, tmp_path):
        path = tmp_path / "r.md"
        path.write_text("---\nname: r\ntools: [Bash, Read]\n---\n\nHello\n")
        role = parse_role_file(path)
        assert role is not None
        assert role.tools == ["Bash", "Read"]


# --- merge_roles ---

class TestMergeRoles:
    def test_empty_roles(self):
        merged = merge_roles([])
        assert merged.tools == set()
        assert merged.disallowed_tools == set()
        assert merged.instructions == ""

    def test_tools_union(self):
        r1 = RoleConfig(name="a", tools=["Bash", "Read"])
        r2 = RoleConfig(name="b", tools=["Read", "Write"])
        merged = merge_roles([r1, r2])
        assert merged.tools == {"Bash", "Read", "Write"}

    def test_disallowed_intersection(self):
        r1 = RoleConfig(name="a", disallowed_tools=["AskUserQuestion", "Edit"])
        r2 = RoleConfig(name="b", disallowed_tools=["AskUserQuestion"])
        merged = merge_roles([r1, r2])
        # Only AskUserQuestion is in both
        assert merged.disallowed_tools == {"AskUserQuestion"}

    def test_disallowed_empty_when_no_overlap(self):
        r1 = RoleConfig(name="a", disallowed_tools=["Edit"])
        r2 = RoleConfig(name="b", disallowed_tools=["Write"])
        merged = merge_roles([r1, r2])
        assert merged.disallowed_tools == set()

    def test_instructions_concatenated(self):
        r1 = RoleConfig(name="a", instructions="Do A.")
        r2 = RoleConfig(name="b", instructions="Do B.")
        merged = merge_roles([r1, r2])
        assert "Do A." in merged.instructions
        assert "Do B." in merged.instructions

    def test_single_role(self):
        r1 = RoleConfig(name="a", tools=["Bash"], disallowed_tools=["Edit"], instructions="Hello")
        merged = merge_roles([r1])
        assert merged.tools == {"Bash"}
        assert merged.disallowed_tools == {"Edit"}
        assert merged.instructions == "Hello"


# --- discover_role ---

class TestDiscoverRole:
    def test_bundled_roles_found(self):
        """All bundled roles should be discoverable."""
        for name in ["agentwire", "voice", "worker", "task-runner", "chatbot", "init", "soul"]:
            path = discover_role(name)
            assert path is not None, f"Bundled role '{name}' not found"

    def test_project_level_overrides_bundled(self, tmp_path):
        # Create project-level role
        project_roles = tmp_path / ".agentwire" / "roles"
        project_roles.mkdir(parents=True)
        custom = project_roles / "agentwire.md"
        custom.write_text("---\nname: agentwire\n---\n\nCustom!\n")

        path = discover_role("agentwire", project_path=tmp_path)
        assert path == custom

    def test_unknown_role_returns_none(self):
        path = discover_role("nonexistent-role-xyz")
        assert path is None


# --- inject_soul ---

class TestInjectSoul:
    def test_appended_last(self):
        assert inject_soul(["agentwire"]) == ["agentwire", "soul"]

    def test_injected_with_explicit_roles(self):
        assert inject_soul(["agentwire", "voice"]) == ["agentwire", "voice", "soul"]

    def test_empty_list_gets_soul(self):
        assert inject_soul([]) == ["soul"]

    def test_headless_roles_excluded(self):
        for headless in ["worker", "task-runner", "notifications"]:
            assert inject_soul([headless]) == [headless]

    def test_headless_mixed_excluded(self):
        assert inject_soul(["agentwire", "worker"]) == ["agentwire", "worker"]

    def test_no_double_add(self):
        assert inject_soul(["soul"]) == ["soul"]
        assert inject_soul(["agentwire", "soul"]) == ["agentwire", "soul"]

    def test_soul_lens_variant_excluded(self):
        # soul-* variants self-exclude the standard soul
        assert inject_soul(["soul-brain"]) == ["soul-brain"]

    def test_council_roles_excluded(self):
        # Council sessions (#213) carry their own lens/synthesis voice
        assert inject_soul(["council-member", "council-brain"]) == [
            "council-member",
            "council-brain",
        ]
        assert inject_soul(["council-orchestrator"]) == ["council-orchestrator"]

    def test_no_soul_flag(self):
        assert inject_soul(["agentwire"], no_soul=True) == ["agentwire"]

    def test_global_opt_out(self):
        config = {"session": {"inject_soul": False}}
        assert inject_soul(["agentwire"], config) == ["agentwire"]

    def test_global_default_enabled(self):
        assert inject_soul(["agentwire"], {}) == ["agentwire", "soul"]
        assert inject_soul(["agentwire"], None) == ["agentwire", "soul"]

    def test_input_not_mutated(self):
        names = ["agentwire"]
        inject_soul(names)
        assert names == ["agentwire"]

    def test_bundled_soul_is_pure_personality(self):
        """soul.md must not widen or narrow tool permissions."""
        path = discover_role("soul")
        assert path is not None
        role = parse_role_file(path)
        assert role is not None
        assert role.name == "soul"
        assert role.tools == []
        assert role.disallowed_tools == []
        assert role.instructions


# --- resolve_roles: the one greppable resolver (#309) ---

class TestResolveRoles:
    def test_intrinsic_etiquette_is_the_zero_config_default(self):
        # Zero-config: each verb's kind yields exactly its intrinsic etiquette.
        assert resolve_roles("orchestrator") == ["orchestrator"]
        assert resolve_roles("worktree-session") == ["worktree-session"]
        assert resolve_roles("worker") == ["worker"]

    def test_cli_roles_replace_intrinsic(self):
        # User owns the list — orchestrator etiquette is NOT forced on top.
        assert resolve_roles("orchestrator", cli_roles=["gh-issues"]) == ["gh-issues"]

    def test_project_roles_replace_intrinsic(self):
        assert resolve_roles("worktree-session", project_roles=["domain"]) == ["domain"]

    def test_cli_wins_over_project(self):
        # --roles takes precedence over .agentwire.yml roles.
        assert resolve_roles("orchestrator", cli_roles=["a"], project_roles=["b"]) == ["a"]

    def test_internal_callers_never_inherit_etiquette(self):
        # council/scheduler/services pass roles → kind is not consulted, so a
        # council session never picks up orchestrator etiquette.
        assert resolve_roles("orchestrator", cli_roles=["council-orchestrator"]) == ["council-orchestrator"]
        assert resolve_roles("worker", cli_roles=["task-runner"]) == ["task-runner"]

    def test_kind_none_no_default(self):
        assert resolve_roles(None) == []
        assert resolve_roles(None, cli_roles=["x"]) == ["x"]

    def test_unknown_kind_no_default(self):
        assert resolve_roles("nope") == []

    def test_soul_appends_separately(self):
        # Resolve, then auto-append: two distinct phases.
        names = inject_soul(resolve_roles("orchestrator"))
        assert names == ["orchestrator", "soul"]

    def test_worker_etiquette_stays_voiceless(self):
        # worker is headless → soul is NOT appended.
        names = inject_soul(resolve_roles("worker"))
        assert names == ["worker"]


class TestIntrinsicEtiquette:
    def test_maps_three_kinds(self):
        assert INTRINSIC_ETIQUETTE == {
            "orchestrator": "orchestrator",
            "worktree-session": "worktree-session",
            "worker": "worker",
        }

    def test_every_intrinsic_role_is_discoverable(self):
        for role_name in INTRINSIC_ETIQUETTE.values():
            path = discover_role(role_name)
            assert path is not None, f"intrinsic role not found: {role_name}"
            role = parse_role_file(path)
            assert role is not None
            assert role.name == role_name
            # Etiquette roles don't widen the tool whitelist (worker narrows it
            # — disallowedTools: AskUserQuestion — which is fine).
            assert role.tools == []
            assert role.instructions


class TestBundledWorktreeSessionRole:
    def test_thin_etiquette_no_pm_no_templating(self):
        """worktree-session is pure orchestration etiquette: no PM, no {{templates}}."""
        role = parse_role_file(discover_role("worktree-session"))
        assert role is not None
        # Etiquette present.
        for needle in ["agentwire rebuild", "portal restart", "DRAFT", "uv run pytest", "agentwire msg send"]:
            assert needle in role.instructions, f"etiquette missing: {needle}"
        # PM ghost and templating gone.
        assert "{{" not in role.instructions
        assert "Closes #" not in role.instructions
        assert "single source of truth" not in role.instructions.lower()
        assert "breadcrumb" not in role.instructions.lower()


# --- council roles (#213) ---

COUNCIL_ROLES = [
    "council-member",
    "council-brain",
    "council-conscience",
    "council-gut",
    "council-critic",
    "council-historian",
    "council-devils-advocate",
    "council-orchestrator",
]


class TestCouncilRoles:
    @pytest.mark.parametrize("name", COUNCIL_ROLES)
    def test_bundled_council_role_is_pure_personality(self, name):
        """Council roles must parse and not widen or narrow tool permissions."""
        path = discover_role(name)
        assert path is not None, f"Bundled role '{name}' not found"
        role = parse_role_file(path)
        assert role is not None
        assert role.name == name
        assert role.tools == []
        assert role.disallowed_tools == []
        assert role.instructions


class TestTtsToolPromptInjection:
    def test_voice_role_gains_capabilities_section(self, monkeypatch):
        import agentwire.roles as roles_mod
        monkeypatch.setattr(roles_mod, "get_tts_tool_prompt",
                            lambda: "Supports inline [laugh] tags.")
        roles, missing = roles_mod.load_roles(["voice", "agentwire"])
        assert missing == []
        voice = next(r for r in roles if r.name == "voice")
        assert "## TTS backend capabilities" in voice.instructions
        assert "Supports inline [laugh] tags." in voice.instructions
        # Non-voice roles untouched
        other = next(r for r in roles if r.name != "voice")
        assert "TTS backend capabilities" not in other.instructions

    def test_no_prompt_no_injection(self, monkeypatch):
        import agentwire.roles as roles_mod
        monkeypatch.setattr(roles_mod, "get_tts_tool_prompt", lambda: "")
        roles, _ = roles_mod.load_roles(["voice"])
        assert "## TTS backend capabilities" not in roles[0].instructions

    def test_get_tts_tool_prompt_default_tier_is_empty(self, monkeypatch, tmp_path):
        import agentwire.roles as roles_mod
        monkeypatch.setattr(roles_mod, "_tts_tool_prompt_cache", None)
        from agentwire.config import load_config
        cfg = load_config(tmp_path / "nonexistent.yaml")  # default tier
        monkeypatch.setattr("agentwire.config.load_config", lambda *a, **k: cfg)
        assert roles_mod.get_tts_tool_prompt() == ""
