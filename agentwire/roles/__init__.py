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
HEADLESS_ROLES = {"worker", "task-runner", "notifications"}


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


# The etiquette intrinsic to each session KIND. The kind is DERIVED from the
# spawn verb — `agentwire new` → orchestrator, `agentwire worktree` →
# worktree-session, `agentwire spawn` → worker — never user-configured. The
# etiquette describes what the session structurally IS (an orchestrator
# delegates; a worker focuses and reports; a worktree session isolates and
# opens a draft PR), so it can't get lost: it's derived, not looked up in a
# defaults table. These are the ONLY roles agentwire injects on its own behalf.
INTRINSIC_ETIQUETTE: dict[str, str] = {
    "orchestrator": "orchestrator",
    "worktree-session": "worktree-session",
    "worker": "worker",
}


def resolve_roles(
    kind: str | None,
    cli_roles: list[str] | None = None,
    project_roles: list[str] | None = None,
) -> list[str]:
    """Resolve a session's role list — the ONE place role precedence lives.

    One sentence: a session's roles are ``--roles``, else ``.agentwire.yml
    roles:``, else the etiquette intrinsic to the spawn verb's KIND.

    The intrinsic etiquette is the *zero-config default* — it kicks in only
    when the user supplied no roles, replacing the old global default-role
    scatter (a config lookup) with a value derived from the verb. When the
    user (or an internal caller like council/scheduler/services) DOES pass
    roles, those are the role list, verbatim — so a council session never
    inherits orchestrator etiquette, a task-runner never inherits worker
    etiquette, etc. No threading of ``kind`` is needed at those call sites:
    because they pass roles, ``kind`` is simply not consulted.

    This is the "resolve" phase only. ``soul`` is auto-appended *separately*
    by :func:`inject_soul` — resolve first, auto-append second, as two
    visibly distinct phases.

    Args:
        kind: Session kind ("orchestrator" | "worktree-session" | "worker"),
            consulted only when no user roles are given. None → no default.
        cli_roles: Roles from ``--roles`` (highest-precedence user source).
        project_roles: Roles from ``.agentwire.yml roles:``.

    Returns:
        The resolved role list (before the soul auto-append).
    """
    if cli_roles:
        return list(cli_roles)
    if project_roles:
        return list(project_roles)
    intrinsic = INTRINSIC_ETIQUETTE.get(kind) if kind else None
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
