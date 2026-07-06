"""Tests for agentwire/roles/__init__.py — Role parsing, merging, discovery."""


import pytest

from agentwire.roles import (
    INTRINSIC_ETIQUETTE,
    SAFETY_RAIL_KINDS,
    WORKTREE_TOPOLOGY_ETIQUETTE,
    RoleConfig,
    derive_session_kind,
    discover_role,
    inject_soul,
    merge_roles,
    parse_role_file,
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
        for name in ["agentwire", "contributor", "voice", "worker", "task-runner", "chatbot", "init", "soul"]:
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

class TestResolveRolesZeroConfig:
    def test_intrinsic_etiquette_is_the_zero_config_default(self):
        # Zero-config: each verb's kind yields exactly its intrinsic etiquette.
        assert resolve_roles("orchestrator") == ["orchestrator"]
        assert resolve_roles("worker") == ["worker"]

    def test_worktree_topology_selects_the_worktree_flavored_worker_role(self):
        # ROLE (worker) is topology-independent; the FILE it resolves to
        # isn't — a worker on its own worktree gets a genuinely different
        # etiquette (isolation/draft-PR/notify) than a pane/main worker
        # (headless, exit-summary/auto-kill).
        assert resolve_roles("worker", worktree_topology=True) == ["worker-worktree"]
        assert resolve_roles("worker", worktree_topology=False) == ["worker"]

    def test_worktree_topology_is_a_no_op_for_orchestrator(self):
        # Orchestrator is topology-invariant — no worktree-specific variant.
        assert resolve_roles("orchestrator", worktree_topology=True) == ["orchestrator"]


class TestResolveRolesPersona:
    """orchestrator (and kind=None) — replaceable persona: --roles > project > intrinsic."""

    def test_cli_roles_replace_intrinsic(self):
        # User owns the list — orchestrator persona is NOT forced on top.
        assert resolve_roles("orchestrator", cli_roles=["gh-issues"]) == ["gh-issues"]

    def test_project_roles_replace_intrinsic(self):
        assert resolve_roles("orchestrator", project_roles=["domain"]) == ["domain"]

    def test_cli_wins_over_project(self):
        assert resolve_roles("orchestrator", cli_roles=["a"], project_roles=["b"]) == ["a"]

    def test_internal_callers_never_inherit_orchestrator(self):
        # council/scheduler/task sessions pass roles → orchestrator persona is replaced.
        assert resolve_roles("orchestrator", cli_roles=["council-orchestrator"]) == ["council-orchestrator"]

    def test_kind_none_no_default(self):
        assert resolve_roles(None) == []
        assert resolve_roles(None, cli_roles=["x"]) == ["x"]

    def test_unknown_kind_no_default(self):
        assert resolve_roles("nope") == []

    def test_soul_appends_separately(self):
        # Resolve, then auto-append: two distinct phases.
        names = inject_soul(resolve_roles("orchestrator"))
        assert names == ["orchestrator", "soul"]


class TestResolveRolesSafetyRail:
    """worker (pane/main topology or worktree topology) — non-overridable
    contract: etiquette always present, user roles STACK."""

    def test_worker_etiquette_always_present_cli_stacks(self):
        # C1: a worker pane in a configured project keeps worker etiquette.
        assert resolve_roles("worker", cli_roles=["domain"]) == ["worker", "domain"]

    def test_worker_etiquette_always_present_project_stacks(self):
        assert resolve_roles("worker", project_roles=["domain"]) == ["worker", "domain"]

    def test_worker_worktree_etiquette_always_present_cli_stacks(self):
        # C2: `worktree foo --roles domain` keeps the worker-worktree contract.
        assert resolve_roles("worker", worktree_topology=True, cli_roles=["domain"]) == ["worker-worktree", "domain"]

    def test_worker_worktree_etiquette_always_present_project_stacks(self):
        # A repo with roles: in .agentwire.yml still gets the safety contract.
        assert resolve_roles("worker", worktree_topology=True, project_roles=["domain"]) == ["worker-worktree", "domain"]

    def test_project_and_cli_both_stack(self):
        assert resolve_roles("worker", cli_roles=["b"], project_roles=["a"]) == ["worker", "a", "b"]

    def test_intrinsic_not_duplicated(self):
        assert resolve_roles("worker", cli_roles=["worker", "extra"]) == ["worker", "extra"]
        assert resolve_roles("worker", worktree_topology=True, project_roles=["worker-worktree"]) == ["worker-worktree"]

    def test_etiquette_survives_even_a_task_runner_role(self):
        # Scheduler worktree dispatch: task-runner stacks ON worker-worktree.
        assert resolve_roles("worker", worktree_topology=True, cli_roles=["task-runner"]) == ["worker-worktree", "task-runner"]

    def test_worker_etiquette_stays_voiceless(self):
        # pane/main-topology worker is headless → soul is NOT appended even after stacking.
        assert inject_soul(resolve_roles("worker", cli_roles=["x"])) == ["worker", "x"]

    def test_worker_worktree_etiquette_keeps_voice(self):
        # Standalone worktree topology is NOT headless — it keeps soul/voice,
        # unlike the pane/main-topology flavor. This is deliberate: the two
        # role files exist precisely because this behavior genuinely differs
        # by topology (see also AskUserQuestion, which worker.md disallows
        # and worker-worktree.md does not — routed to the parent instead).
        assert inject_soul(resolve_roles("worker", worktree_topology=True)) == ["worker-worktree", "soul"]


class TestDeriveSessionKind:
    def test_explicit_kind_wins(self):
        assert derive_session_kind(True, "worker") == "worker"
        assert derive_session_kind(False, "worker") == "worker"
        # The scheduler overrides the derived worker with a replaceable
        # orchestrator so its task-runner roles win (no agent PR).
        assert derive_session_kind(True, "orchestrator") == "orchestrator"

    def test_branch_means_worker(self):
        # `new project/branch`, portal worktree dispatch (C3) — no explicit kind.
        assert derive_session_kind(True) == "worker"

    def test_plain_name_means_orchestrator(self):
        assert derive_session_kind(False) == "orchestrator"


class TestSchedulerWorktreeOptsOutOfPrEtiquette:
    """The scheduler is the deterministic PR finalizer; its task agents must
    NOT open their own PRs (they'd escape reap_worktree_prs and leak). It
    dispatches `new -s proj/branch --kind orchestrator --roles task-runner`."""

    def test_scheduler_task_has_no_worker_worktree_role(self):
        kind = derive_session_kind(has_branch=True, explicit_kind="orchestrator")
        roles = resolve_roles(kind, worktree_topology=True, cli_roles=["task-runner"])
        assert "worker-worktree" not in roles  # the PR-opening contract
        assert roles == ["task-runner"]

    def test_scheduler_task_without_roles_still_no_pr_etiquette(self):
        # Even a role-less scheduler task gets the orchestrator persona, which
        # carries no draft-PR instruction.
        kind = derive_session_kind(has_branch=True, explicit_kind="orchestrator")
        assert "worker-worktree" not in resolve_roles(kind, worktree_topology=True)

    def test_human_worktree_keeps_full_pr_etiquette(self):
        # `agentwire worktree foo` → cmd_worktree passes kind="worker" (default)
        # and an explicit worktree_topology=True override. The draft-PR/notify
        # contract stays.
        kind = derive_session_kind(has_branch=False, explicit_kind="worker")
        assert "worker-worktree" in resolve_roles(kind, worktree_topology=True, cli_roles=["domain"])


class TestIntrinsicEtiquette:
    def test_maps_two_kinds(self):
        assert INTRINSIC_ETIQUETTE == {
            "orchestrator": "orchestrator",
            "worker": "worker",
        }

    def test_worktree_topology_etiquette_overrides_worker_only(self):
        # Only "worker" has a topology-specific variant; orchestrator doesn't.
        assert WORKTREE_TOPOLOGY_ETIQUETTE == {"worker": "worker-worktree"}

    def test_safety_rail_kinds(self):
        assert SAFETY_RAIL_KINDS == {"worker"}

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

    def test_every_worktree_topology_role_is_discoverable(self):
        for role_name in WORKTREE_TOPOLOGY_ETIQUETTE.values():
            path = discover_role(role_name)
            assert path is not None, f"worktree-topology role not found: {role_name}"
            role = parse_role_file(path)
            assert role is not None
            assert role.name == role_name
            assert role.tools == []
            assert role.instructions


class TestBundledWorkerWorktreeRole:
    def test_thin_etiquette_no_pm_no_templating(self):
        """worker-worktree is pure orchestration etiquette: no PM, no {{templates}}."""
        role = parse_role_file(discover_role("worker-worktree"))
        assert role is not None
        # Etiquette present.
        for needle in ["agentwire rebuild", "portal restart", "DRAFT", "uv run pytest", "agentwire msg send"]:
            assert needle in role.instructions, f"etiquette missing: {needle}"
        # PM ghost and templating gone.
        assert "{{" not in role.instructions
        assert "Closes #" not in role.instructions
        assert "single source of truth" not in role.instructions.lower()
        # No AskUserQuestion lockout (unlike pane worker.md) — routed to the
        # parent via prompt-routing instead, and no disallowedTools frontmatter.
        assert "AskUserQuestion" not in (role.disallowed_tools or [])
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
