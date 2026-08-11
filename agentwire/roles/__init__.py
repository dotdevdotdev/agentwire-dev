"""Role file parsing and merging for composable roles."""

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RoleConfig:
    """Configuration for a single role parsed from a markdown file."""

    name: str
    description: str = ""
    instructions: str = ""  # markdown body after frontmatter
    tools: list[str] = field(default_factory=list)  # whitelist
    disallowed_tools: list[str] = field(default_factory=list)  # blacklist
    color: str | None = None  # UI hint


@dataclass
class MergedRole:
    """Result of merging multiple roles together."""

    tools: set[str]  # union of all tools
    disallowed_tools: set[str]  # intersection (only block if ALL agree)
    instructions: str  # concatenated


def parse_role_file(path: Path) -> RoleConfig | None:
    """Parse a role markdown file with YAML frontmatter.

    Expected format:
        ---
        name: worker
        description: Autonomous code execution
        disallowedTools: AskUserQuestion
        model: inherit
        ---

        # Role instructions here...

    Beta-gated regions (``<!-- beta:flag -->``, see :mod:`agentwire.beta`) are
    resolved HERE rather than at any single call site, because this is the one
    funnel every role reader in the tree goes through — ``load_roles`` (session launch), ``agentwire
    roles list``, ``agentwire role show``, the MCP ``role_show``. A gate
    applied at the launch path only would leave ``role show`` describing a
    prompt no session receives.

    Args:
        path: Path to the role markdown file

    Returns:
        RoleConfig if parsing succeeds, None if file doesn't exist or is invalid
    """
    if not path.exists():
        return None

    try:
        content = path.read_text()
    except Exception:
        return None

    from ..beta import render as render_beta

    content = render_beta(content)

    # Parse YAML frontmatter
    frontmatter = {}
    instructions = content

    # Check for YAML frontmatter (starts with ---)
    if content.startswith("---"):
        # Find closing ---
        end_match = re.search(r"\n---\s*\n", content[3:])
        if end_match:
            yaml_content = content[3:3 + end_match.start()]
            instructions = content[3 + end_match.end():]

            # Simple YAML parsing (handles key: value and key: [list])
            for line in yaml_content.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip()
                    value = value.strip()

                    # Handle list values
                    if value.startswith("[") and value.endswith("]"):
                        # Parse simple array: [item1, item2]
                        items = value[1:-1].split(",")
                        value = [item.strip().strip("'\"") for item in items if item.strip()]
                    elif value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]

                    frontmatter[key] = value

    # Extract fields from frontmatter
    name = frontmatter.get("name", path.stem)
    description = frontmatter.get("description", "")
    color = frontmatter.get("color")

    # Handle tools (can be string or list)
    tools_raw = frontmatter.get("tools", [])
    if isinstance(tools_raw, str):
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
    else:
        tools = tools_raw

    # Handle disallowedTools (can be string or list)
    disallowed_raw = frontmatter.get("disallowedTools", [])
    if isinstance(disallowed_raw, str):
        disallowed_tools = [t.strip() for t in disallowed_raw.split(",") if t.strip()]
    else:
        disallowed_tools = disallowed_raw

    return RoleConfig(
        name=name,
        description=description,
        instructions=instructions.strip(),
        tools=tools,
        disallowed_tools=disallowed_tools,
        color=color,
    )


def merge_roles(roles: list[RoleConfig]) -> MergedRole:
    """Merge multiple roles into a single configuration.

    Merge logic:
    - tools: Union of all tools (deduplicated) - every tool any role needs is available
    - disallowed_tools: Intersection - only block if ALL roles agree
    - instructions: Concatenated with newlines

    Args:
        roles: List of RoleConfig objects to merge

    Returns:
        MergedRole with combined configuration
    """
    if not roles:
        return MergedRole(tools=set(), disallowed_tools=set(), instructions="")

    # Union of all tools (deduplicated)
    tools: set[str] = set()
    for r in roles:
        if r.tools:
            tools.update(r.tools)

    # Intersection of disallowed tools - only block if ALL roles agree
    disallowed: set[str] | None = None
    for r in roles:
        if r.disallowed_tools:
            if disallowed is None:
                disallowed = set(r.disallowed_tools)
            else:
                disallowed &= set(r.disallowed_tools)
    disallowed = disallowed or set()

    # Concatenate instructions
    instructions = "\n\n".join(r.instructions for r in roles if r.instructions)

    return MergedRole(
        tools=tools,
        disallowed_tools=disallowed,
        instructions=instructions,
    )


# Roles whose whole job is autonomous execution — personality actively
# conflicts with them ("run, don't ask"), so soul is never injected.
HEADLESS_ROLES = {"worker", "reviewer", "task-runner", "notifications"}


def inject_soul(role_names: list[str], config: dict | None = None, no_soul: bool = False) -> list[str]:
    """Append the bundled soul role to a session's role list.

    Soul is the always-present default personality (tone, restraint,
    ask-vs-proceed). It rides last so it gets recency weight in the merged
    prompt, while a deliberately-appended stricter role can still override it.

    Skipped when:
    - no_soul is True (per-session --no-soul flag)
    - config disables it globally (session.inject_soul: false)
    - any role is headless (HEADLESS_ROLES — executors stay voiceless)
    - a soul role is already present (soul itself, or a soul-* lens variant)
    - a council-* role is present (council sessions carry their own lens or
      synthesis voice — the standard soul would blur the decomposition)

    Args:
        role_names: Resolved role names for the session
        config: Main config dict (from load_config()), or None to skip the check
        no_soul: Per-session opt-out

    Returns:
        role_names with "soul" appended last, or unchanged if excluded
    """
    if no_soul:
        return role_names
    if config is not None and not config.get("session", {}).get("inject_soul", True):
        return role_names
    if any(r in HEADLESS_ROLES for r in role_names):
        return role_names
    if any(r == "soul" or r.startswith("soul-") or r.startswith("council-") for r in role_names):
        return role_names
    return [*role_names, "soul"]


# ROLE ∈ {orchestrator, worker, reviewer} is authority + etiquette — what the
# session IS and what it's allowed to do — and says nothing about WHERE it
# runs. TOPOLOGY (main checkout / worktree branch / pane) is a separate axis.
# A worker's concrete etiquette payload still differs by topology (a worktree
# worker pushes a branch and opens a draft PR, keeps voice, and can ask via
# prompt-routing; a pane/main-topology worker is headless, writes an
# exit-summary, and gets auto-killed) — that composition lives in
# WORKTREE_TOPOLOGY_ETIQUETTE + _intrinsic_role_name below, not in the kind
# itself. Kind is derived from the spawn verb — `agentwire new`/`worktree` →
# orchestrator or worker (never user-configured except via --kind); `spawn`
# → worker, always pane topology. `reviewer` is never derived, only ever
# explicit (#827) — a worker's mandatory-PR rail inverted: adversarially
# reviews a sibling's PR and never opens/merges its own. These are the ONLY
# roles agentwire injects on its own behalf.
INTRINSIC_ETIQUETTE: dict[str, str] = {
    "orchestrator": "orchestrator",
    "worker": "worker",
    "reviewer": "reviewer",
}

# Override for role=worker/reviewer on worktree topology (its own
# branch/worktree, not a pane): isolation/verify + a role-specific finish
# (worker: draft-PR/notify; reviewer: pull the sibling's branch in for local
# e2e, never push/PR/merge). "orchestrator" is topology-invariant — no
# worktree-specific variant.
WORKTREE_TOPOLOGY_ETIQUETTE: dict[str, str] = {
    "worker": "worker-worktree",
    "reviewer": "reviewer-worktree",
}

# SAFETY-RAIL kinds carry a STRUCTURAL contract that must always be present —
# it describes what the session *is*, and dropping it is a safety regression.
# For these, the intrinsic etiquette is NON-OVERRIDABLE: user/project roles
# STACK on top of it, never replace it. worker's rail is "must open a PR";
# reviewer's is the inverse — "must never open/merge one, never patch the
# branch under review directly" (#827) — equally worth protecting from a
# later --roles silently erasing it.
#
# Every other kind (orchestrator) is a PERSONA: a sensible zero-config default
# that explicit roles are free to REPLACE — that's what keeps council /
# scheduler / task sessions clean when they pass their own roles.
SAFETY_RAIL_KINDS: set[str] = {"worker", "reviewer"}


def _intrinsic_role_name(kind: str | None, worktree_topology: bool) -> str | None:
    """Which bundled role file backs a given (kind, topology) pair."""
    if kind is None:
        return None
    if worktree_topology and kind in WORKTREE_TOPOLOGY_ETIQUETTE:
        return WORKTREE_TOPOLOGY_ETIQUETTE[kind]
    return INTRINSIC_ETIQUETTE.get(kind)


def derive_session_kind(has_branch: bool, explicit_kind: str | None = None) -> str:
    """The session's ROLE for an `agentwire new` (or worktree) dispatch.

    Role is derived from what's being created, never user-configured:
    - An explicit kind wins — including "reviewer", which this function never
      derives on its own (there's no signal in has_branch that means
      "review", so it's explicit-only).
    - Otherwise a worktree (a ``project/branch`` name, which is also how the
      scheduler and portal dispatch worktrees) is a subordinate — "worker" —
      regardless of entrypoint. A plain name is an orchestrator.

    This is the ROLE axis only (authority: orchestrator vs worker vs
    reviewer) — it says nothing about topology. The concrete etiquette
    payload for a worker/reviewer still varies by topology (worktree vs
    pane/main); that composition happens in :func:`resolve_roles`, not here.
    """
    if explicit_kind:
        return explicit_kind
    return "worker" if has_branch else "orchestrator"


def resolve_roles(
    kind: str | None,
    *,
    worktree_topology: bool = False,
    cli_roles: list[str] | None = None,
    project_roles: list[str] | None = None,
) -> list[str]:
    """Resolve a session's role list — the ONE place role precedence lives.

    Two rules, by kind:

    - **Safety-rail kinds** (``worker``, ``reviewer``): the intrinsic
      etiquette is structural and non-overridable. Result = intrinsic +
      project roles + cli roles, stacked and de-duplicated (etiquette always
      first/present). ``--roles`` ADDS to the contract, never removes it.
      Which etiquette file is intrinsic depends on ``worktree_topology`` — a
      worker on its own worktree gets the isolation/draft-PR/notify contract;
      a pane (or main-topology) worker gets the exit-summary/auto-kill
      contract. Reviewer mirrors the same topology split, inverted: never
      opens/merges a PR, reports a verdict via notify_parent instead.
    - **Persona kind** (``orchestrator``, and ``kind=None``): the intrinsic
      etiquette is just a zero-config default. Precedence ``--roles`` >
      ``.agentwire.yml roles:`` > intrinsic — user roles REPLACE it. So a
      council/task session that passes its own roles never inherits
      orchestrator etiquette.

    This is the "resolve" phase only. ``soul`` is auto-appended *separately*
    by :func:`inject_soul` — resolve first, auto-append second, as two
    visibly distinct phases.

    Args:
        kind: Session kind ("orchestrator" | "worker" | "reviewer"), or None
            (treated as a persona with no default).
        worktree_topology: True when this is a standalone session on its own
            git worktree/branch (vs a pane, or a plain main-topology
            session) — selects which "worker"/"reviewer" etiquette file
            applies.
        cli_roles: Roles from ``--roles`` (highest-precedence user source).
        project_roles: Roles from ``.agentwire.yml roles:``.

    Returns:
        The resolved role list (before the soul auto-append).
    """
    intrinsic = _intrinsic_role_name(kind, worktree_topology)

    if kind in SAFETY_RAIL_KINDS:
        # Non-overridable contract: etiquette always present, user roles stack.
        roles: list[str] = [intrinsic] if intrinsic else []
        for r in (project_roles or []):
            if r not in roles:
                roles.append(r)
        for r in (cli_roles or []):
            if r not in roles:
                roles.append(r)
        return roles

    # Persona: replaceable default.
    if cli_roles:
        return list(cli_roles)
    if project_roles:
        return list(project_roles)
    return [intrinsic] if intrinsic else []


def discover_role(name: str, project_path: Path | None = None) -> Path | None:
    """Find a role file by name using discovery order.

    Discovery order (first match wins):
    1. Project: .agentwire/roles/{name}.md
    2. User: ~/.agentwire/roles/{name}.md
    3. Bundled: agentwire/roles/{name}.md (package)

    Args:
        name: Role name (without .md extension)
        project_path: Optional project directory for project-level roles

    Returns:
        Path to role file if found, None otherwise
    """
    # 1. Project roles
    if project_path:
        project_role = project_path / ".agentwire" / "roles" / f"{name}.md"
        if project_role.exists():
            return project_role

    # 2. User roles
    user_role = Path.home() / ".agentwire" / "roles" / f"{name}.md"
    if user_role.exists():
        return user_role

    # 3. Bundled roles (in package)
    import importlib.resources
    try:
        files = importlib.resources.files("agentwire.roles")
        role_path = files.joinpath(f"{name}.md")
        if role_path.is_file():
            return Path(str(role_path))
    except Exception:
        pass

    return None


_tts_tool_prompt_cache: str | None = None


def get_tts_tool_prompt() -> str:
    """Shim-authored `tool_prompt` from a custom TTS shim's /capabilities.

    Cached per process (fail-soft, 1.5s timeout). Injected into the `voice`
    role so sessions learn model-specific tags/instructions — the producer
    end of the capability loop.
    """
    global _tts_tool_prompt_cache
    if _tts_tool_prompt_cache is not None:
        return _tts_tool_prompt_cache
    try:
        import json
        import urllib.request

        from ..config import load_config

        cfg = load_config()
        if cfg.tts.backend != "custom" or not cfg.tts.url:
            _tts_tool_prompt_cache = ""
            return ""
        with urllib.request.urlopen(f"{cfg.tts.url.rstrip('/')}/capabilities", timeout=1.5) as r:
            _tts_tool_prompt_cache = (json.load(r).get("tool_prompt") or "").strip()
    except Exception:
        _tts_tool_prompt_cache = ""
    return _tts_tool_prompt_cache


def load_roles(
    role_names: list[str],
    project_path: Path | None = None,
) -> tuple[list[RoleConfig], list[str]]:
    """Load multiple roles by name.

    Args:
        role_names: List of role names to load
        project_path: Optional project directory for project-level roles

    Returns:
        Tuple of (loaded roles, missing role names)
    """
    roles: list[RoleConfig] = []
    missing: list[str] = []

    for name in role_names:
        path = discover_role(name, project_path)
        if path:
            role = parse_role_file(path)
            if role:
                roles.append(role)
            else:
                missing.append(name)
        else:
            missing.append(name)

    # Teach the voice role what the configured TTS shim accepts (emotion
    # tags, style instructions). Single chokepoint — covers every session
    # creation path without touching the call sites.
    prompt = get_tts_tool_prompt()
    if prompt:
        for role in roles:
            if role.name == "voice":
                role.instructions += f"\n\n## TTS backend capabilities\n{prompt}"

    return roles, missing
