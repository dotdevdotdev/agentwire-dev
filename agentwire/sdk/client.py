"""Agentwire SDK client option builder.

Composes `ClaudeAgentOptions` for any consumer that wraps `ClaudeSDKClient`
(REPL print mode, Textual REPL, fan-out columns, future composite views).
The runner-side option-builder for workflow nodes lives in
`workflows/runners/anthropic.py` because nodes have a different config
shape (max_thinking_tokens, max_budget_usd, task_budget_tokens, betas).

Defaults: claude-opus-4-7 + adaptive thinking + effort=high. Mode-derived
permission + tool surface match the `sdk-bypass` / `sdk-prompted` /
`sdk-restricted` session-type variants documented in
docs/missions/agentwire-repl.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from agentwire.repl.context import SessionContext
from agentwire.sdk.damage_control import make_pre_tool_hook


PERMISSION_MODE_MAP = {
    "bypass": "bypassPermissions",
    "prompted": "default",
    "restricted": "plan",
}

# Tool surface per variant. Restricted is read-only (no Write/Edit/Bash).
FULL_TOOLS = ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch"]
RESTRICTED_TOOLS = ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]

DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_EFFORT = "high"
DEFAULT_THINKING_MODE = "adaptive"

# Agentwire MCP server, auto-attached to every REPL session. ~87 agentwire
# tools (panes, voice, scheduler, channels, etc.) available first-class
# without per-session configuration. Set AGENTWIRE_REPL_MCP=0 to opt out.
MCP_SERVER_NAME = "agentwire"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}"


def _agentwire_mcp_config() -> dict:
    """Stdio MCP server config that re-invokes this same Python interpreter."""
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "agentwire", "mcp"],
    }


def _mcp_enabled() -> bool:
    return os.environ.get("AGENTWIRE_REPL_MCP", "1") != "0"


def _damage_control_enabled() -> bool:
    """`AGENTWIRE_REPL_DAMAGE_CONTROL=0` opts out (used by tests)."""
    return os.environ.get("AGENTWIRE_REPL_DAMAGE_CONTROL", "1") != "0"


def thinking_config(mode: str) -> dict | None:
    """Translate the REPL's `/thinking` mode into an SDK thinking config.

    `adaptive` defaults to `display: "summarized"` so users see reasoning
    progress rather than a long silent pause — Opus 4.7 omits thinking
    content by default. Set `/thinking off` to disable thinking entirely.
    """
    if mode == "adaptive":
        return {"type": "adaptive", "display": "summarized"}
    if mode == "summarized":
        return {"type": "adaptive", "display": "summarized"}
    if mode == "off":
        return {"type": "disabled"}
    return {"type": "adaptive", "display": "summarized"}


def _find_ancestor_file(start: Path, name: str) -> Path | None:
    for ancestor in [start, *start.parents]:
        candidate = ancestor / name
        if candidate.is_file():
            return candidate
    return None


def build_options(
    ClaudeAgentOptions: Any,
    mode: str,
    model: str | None,
    system_prompt: str | None,
    cwd: Path | None = None,
    resume_sdk_session_id: str | None = None,
    effort: str = DEFAULT_EFFORT,
    thinking_mode: str = DEFAULT_THINKING_MODE,
    can_use_tool: Any = None,
    session_context: SessionContext | None = None,
) -> Any:
    """Compose `ClaudeAgentOptions` for a REPL-style session.

    Permission mode + tool surface are derived from the session-type variant
    (`sdk-bypass`/`sdk-prompted`/`sdk-restricted`). System prompt layers
    project CLAUDE.md + AGENTS.md + an optional explicit append.
    `resume_sdk_session_id` passes through to the SDK's `resume` field to
    continue a prior conversation. Damage-control hooks attach
    automatically (gated on `AGENTWIRE_REPL_DAMAGE_CONTROL`).
    """
    base_tools = RESTRICTED_TOOLS if mode == "restricted" else FULL_TOOLS
    allowed = list(base_tools)
    mcp_servers: dict[str, Any] = {}
    if _mcp_enabled():
        mcp_servers[MCP_SERVER_NAME] = _agentwire_mcp_config()
        # `mcp__<server>` allows all tools from that server (Claude Code convention).
        allowed.append(MCP_TOOL_PREFIX)

    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "permission_mode": PERMISSION_MODE_MAP.get(mode, "bypassPermissions"),
        "allowed_tools": allowed,
        "setting_sources": ["user"],       # load ~/.claude/hooks — damage-control
        # Partial messages stream incremental thinking text and tool input
        # as it's generated. Without this, an Opus 4.7 turn that writes a
        # 10KB HTML file inside a Write tool input shows nothing for ~120s
        # between [→ Write ...] and the final result.
        "include_partial_messages": True,
        "effort": effort,
        "thinking": thinking_config(thinking_mode),
    }
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if resume_sdk_session_id:
        kwargs["resume"] = resume_sdk_session_id
    if can_use_tool is not None:
        kwargs["can_use_tool"] = can_use_tool

    # Python-side damage control. Mirrors the shell hooks at
    # ~/.agentwire/hooks/damage-control/*.py, but runs in-process via the
    # SDK's PreToolUse callback so direct SDK tool dispatch can't bypass it.
    if _damage_control_enabled():
        try:
            from claude_agent_sdk import HookMatcher
        except ImportError:
            HookMatcher = None  # SDK absent → render path will refuse anyway
        if HookMatcher is not None:
            hook = make_pre_tool_hook(mode=mode)
            if hook is not None:
                kwargs["hooks"] = {
                    "PreToolUse": [
                        HookMatcher(
                            matcher="Bash|Edit|MultiEdit|Write",
                            hooks=[hook],
                        )
                    ]
                }

    append_parts: list[str] = []
    # Roles first — they're identity and tool-permission posture, so they
    # frame everything that follows. Then CLAUDE.md / AGENTS.md (project
    # facts), then explicit override.
    if session_context is not None and session_context.role_instructions:
        append_parts.append(
            f"--- roles: {', '.join(session_context.role_names)} ---\n"
            f"{session_context.role_instructions}"
        )
    if cwd is not None:
        for name in ("CLAUDE.md", "AGENTS.md"):
            found = _find_ancestor_file(cwd, name)
            if found is not None:
                try:
                    append_parts.append(f"--- {found} ---\n{found.read_text()}")
                except Exception:
                    pass
    if system_prompt:
        append_parts.append(system_prompt)

    if append_parts:
        kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": "\n\n".join(append_parts),
        }

    return ClaudeAgentOptions(**kwargs)
