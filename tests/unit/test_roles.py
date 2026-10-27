"""Tests for agentwire/roles/__init__.py — Role parsing, merging, discovery."""



from agentwire.roles import (
    INTRINSIC_ETIQUETTE,
    derive_session_kind,
    discover_role,
    parse_role_file,
    resolve_roles,
)




# --- parse_role_file ---



# --- merge_roles ---



# --- discover_role ---



# --- inject_soul ---



# --- resolve_roles: the one greppable resolver (#309) ---

class TestResolveRolesZeroConfig:

    def test_worktree_topology_selects_the_worktree_flavored_worker_role(self):
        # ROLE (worker) is topology-independent; the FILE it resolves to
        # isn't — a worker on its own worktree gets a genuinely different
        # etiquette (isolation/draft-PR/notify) than a pane/main worker
        # (headless, exit-summary/auto-kill).
        assert resolve_roles("worker", worktree_topology=True) == ["worker-worktree"]
        assert resolve_roles("worker", worktree_topology=False) == ["worker"]




class TestResolveRolesPersona:
    """orchestrator (and kind=None) — replaceable persona: --roles > project > intrinsic."""









class TestResolveRolesSafetyRail:
    """worker (pane/main topology or worktree topology) — non-overridable
    contract: etiquette always present, user roles STACK."""

    def test_worker_etiquette_always_present_cli_stacks(self):
        # C1: a worker pane in a configured project keeps worker etiquette.
        assert resolve_roles("worker", cli_roles=["domain"]) == ["worker", "domain"]










class TestResolveRolesSafetyRailReviewer:
    """reviewer (#827) — worker's non-overridable contract, inverted: never
    opens/merges a PR instead of always opening one. Same stacking shape."""








    def test_custom_roles_can_never_erase_the_never_merge_contract(self):
        # The whole point of a dedicated kind over a --roles bundle (#827):
        # no combination of user/project roles can drop the intrinsic
        # reviewer etiquette the way it could on a replaceable persona kind.
        roles = resolve_roles("reviewer", worktree_topology=True,
                               cli_roles=["some-custom-role"], project_roles=["another"])
        assert roles[0] == "reviewer-worktree"
        assert "reviewer-worktree" in roles




class TestSchedulerWorktreeOptsOutOfPrEtiquette:
    """The scheduler is the deterministic PR finalizer; its task agents must
    NOT open their own PRs (they'd escape reap_worktree_prs and leak). It
    dispatches `new -s proj/branch --kind orchestrator --roles task-runner`."""

    def test_scheduler_task_has_no_worker_worktree_role(self):
        kind = derive_session_kind(has_branch=True, explicit_kind="orchestrator")
        roles = resolve_roles(kind, worktree_topology=True, cli_roles=["task-runner"])
        assert "worker-worktree" not in roles  # the PR-opening contract
        assert roles == ["task-runner"]




class TestIntrinsicEtiquette:



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




