"""Python-side damage control for the agentwire SDK REPL.

Mirror of `~/.agentwire/hooks/damage-control/*` for the SDK. Shell hooks
fire for tools that go through the bundled `claude` binary, but the REPL's
SDK can call tools directly in Python — so we run the same pattern checks
in-process via a `PreToolUse` SDK hook.

Single source of truth: `~/.agentwire/hooks/damage-control/patterns.yaml`.
The shell scripts also load this file, so updates land in both places.

Returns shape match the SDK's PreToolUseHookSpecificOutput
(`decision: 'block' | 'allow' | 'ask'`). The hook is wired in
`agentwire.repl.app.build_options`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

DEFAULT_PATTERNS_PATH = Path.home() / ".agentwire" / "hooks" / "damage-control" / "patterns.yaml"


_TOOL_TO_KEY = {
    "Bash": "bashToolPatterns",
    "Edit": "editToolPatterns",
    "Write": "writeToolPatterns",
    "MultiEdit": "editToolPatterns",
}


def load_patterns(path: Path | None = None) -> dict:
    """Load and parse the patterns YAML. Returns empty dict on any failure.

    Damage control should never bring the REPL down — if patterns can't
    load, the SDK still has its own permission_mode gate and the user's
    shell hooks (if installed) still fire from the bundled binary.
    """
    target = path or DEFAULT_PATTERNS_PATH
    try:
        import yaml  # local import: pyyaml is already a hooks dep
    except ImportError:
        return {}
    try:
        return yaml.safe_load(target.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def check_bash(command: str, patterns: dict, *, mode: str) -> tuple[str, str] | None:
    """Match `command` against bashToolPatterns. Returns (decision, reason) or None.

    `decision` is 'deny' for hard rules and 'ask' for rules with `ask: true`.
    `bypassable: true` rules pass through silently when mode == 'bypass'
    (user explicitly opted into that semantics by picking sdk-bypass) and
    block otherwise.

    'deny' / 'ask' match the SDK's PreToolUseHookSpecificOutput contract.
    """
    if not command:
        return None
    rules = patterns.get("bashToolPatterns") or []
    for rule in rules:
        pattern = rule.get("pattern")
        if not pattern:
            continue
        try:
            if not re.search(pattern, command):
                continue
        except re.error:
            continue
        bypassable = bool(rule.get("bypassable"))
        ask = bool(rule.get("ask"))
        reason = rule.get("reason") or "blocked by damage-control"
        if bypassable and mode == "bypass":
            return None
        if ask:
            return "ask", reason
        return "deny", reason
    return None


def check_path(file_path: str, patterns: dict, key: str) -> tuple[str, str] | None:
    """Edit/Write path checks. Returns ('deny', reason) or None."""
    if not file_path:
        return None
    rules = patterns.get(key) or []
    for rule in rules:
        pattern = rule.get("pattern")
        if not pattern:
            continue
        try:
            if not re.search(pattern, file_path):
                continue
        except re.error:
            continue
        return "deny", rule.get("reason") or "blocked by damage-control"
    return None


def make_pre_tool_hook(*, mode: str, patterns_path: Path | None = None):
    """Build the SDK `PreToolUse` hook callback.

    The hook runs in-process before each tool call, regardless of whether
    the SDK's bundled binary would have surfaced the same shell hook. This
    is the safety net Phase 3 PR 2 promises in the mission doc — SDK tool
    calls bypass `~/.claude/hooks/*.sh`, so we re-implement the patterns
    here.
    """
    patterns = load_patterns(patterns_path)
    if not patterns:
        return None

    async def _hook(input_data: dict, tool_use_id: str | None, ctx: Any):
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}
        decision: tuple[str, str] | None = None

        if tool_name == "Bash":
            decision = check_bash(tool_input.get("command", ""), patterns, mode=mode)
        elif tool_name in ("Edit", "MultiEdit"):
            decision = check_path(
                tool_input.get("file_path", ""),
                patterns,
                _TOOL_TO_KEY[tool_name],
            )
        elif tool_name == "Write":
            decision = check_path(
                tool_input.get("file_path", ""),
                patterns,
                _TOOL_TO_KEY[tool_name],
            )

        if decision is None:
            return {}
        verdict, reason = decision
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": verdict,
                "permissionDecisionReason": f"damage-control: {reason}",
            }
        }

    return _hook
