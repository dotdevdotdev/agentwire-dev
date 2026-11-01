"""Claude Code adapter (#1209 M3): hook payload -> ``ToolCall``/``Context``, ``Verdict`` -> exit code.

The five PreToolUse hooks differ only in WHICH tools they handle and how their
rule sources load; every one is ``parse -> decide -> render``, and this file holds the
parse and render halves (``policy.Guard`` is the decide half). A hook declares a tool
table ``{tool_name: (kind, (payload fields...))}`` and calls these in order.

``render`` is the only place a decision becomes an exit code, a stderr line, an audit row
and a notification, so a verdict that reaches it can only be rendered one way.

Standard library only; inlined into the hooks after ``policy.py`` (see ``policy.py`` for
why the engine arrives as an argument).
"""

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# ``permission_mode`` values that mean the human opted out of ask-escalation friction:
#   - "bypassPermissions" -> --dangerously-skip-permissions
#   - "auto"              -> autonomous /loop or similar auto mode
# Hard blocks still fire either way; an ``ask`` becomes an allow — EXCEPT an unverifiable one.
BYPASS_PERMISSION_MODES = frozenset({"bypassPermissions", "auto"})

# stdout of an ask: Claude Code's PreToolUse permission-decision envelope.
_HOOK_EVENT = "PreToolUse"


def _policy() -> Tuple[Any, Any, str]:
    """``(ToolCall, Context, BYPASS)``: inlined above this file in a hook, else ``policy.py``."""
    g = globals()
    if "ToolCall" in g:
        return g["ToolCall"], g["Context"], g["BYPASS"]
    mod = importlib.import_module(f"{__package__}.policy")
    return mod.ToolCall, mod.Context, mod.BYPASS


def load_hook_config(engine: Any, hook_file: str, tooldefs: bool) -> Dict[str, Any]:
    """The merged rule set for the hook at ``hook_file``, plus the effective safety knobs.

    Rules load from the sources ``engine.resolve_rule_sources`` names, relative to the
    directory the hook sits in. ``tooldefs`` is True for the hooks that judge shell commands.
    """
    bundle = Path(hook_file).parent
    sources = engine.resolve_rule_sources(bundle)
    tooldef_sources = engine.resolve_tooldef_sources(bundle.parent.parent / "tooldefs") if tooldefs else None
    config = engine.load_config(sources, tooldef_sources)
    config["safety"] = engine.load_safety_config()
    return config


def parse_payload(payload: Dict[str, Any], tools: Optional[Dict[str, Tuple[str, Tuple[str, ...]]]],
                  engine: Any) -> Tuple[Any, Any]:
    """``(ToolCall | None, Context)``; no call means this hook has nothing to judge.

    ``tools`` maps a tool name to ``(kind, fields)``: for ``shell`` the field names the
    command, for read/write/edit the target path (the first non-empty field wins). ``None``
    means every tool is an ``mcp`` call.
    """
    ToolCall, Context, BYPASS = _policy()
    # Session attribution for audit rows (#940): hand the parsed payload to audit_logger
    # so every log_* call carries the conversation id. Fail-open.
    audit_logger = sys.modules.get("audit_logger")  # the hook imported it (or the import failed)
    if audit_logger is not None:
        audit_logger.HOOK_INPUT = payload

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    ctx = Context(
        mode=BYPASS if payload.get("permission_mode", "") in BYPASS_PERMISSION_MODES else "default",
        unattended=engine.is_unattended(),
    )
    if tools is None:
        return ToolCall("mcp", tool_name, tool_input=tool_input or {}), ctx
    if tool_name not in tools:
        return None, ctx
    kind, fields = tools[tool_name]
    value = next((tool_input.get(f, "") for f in fields if tool_input.get(f, "")), "")
    if not value:
        return None, ctx
    if kind == "shell":
        return ToolCall(kind, tool_name, command=value, cwd=payload.get("cwd")), ctx
    return ToolCall(kind, tool_name, paths=(value,)), ctx


def notify_unattended_block(engine: Any, subject: str, reason: str, rule_id: Optional[str]) -> None:
    """Fire-and-forget: tell the owner an unattended action was blocked.

    Runs the profile's notifier argv, detached and non-blocking so it never sits on the
    hook's hot path; failures are swallowed (the block has already fired and is
    audit-logged regardless). A hook is stdlib-only and cannot import the host, hence the
    subprocess.
    """
    argv = engine.PROFILE.unattended_block_notifier
    if not argv:
        return
    try:
        subprocess.Popen(
            [*argv, "--reason", reason or "", "--rule-id", rule_id or "", "--command", subject[:500]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def render(verdict: Any, engine: Any) -> None:
    """Carry out ``verdict`` and exit: audit row, notification, stderr, then the exit code / ask envelope."""
    if verdict.audit:
        event, args, kwargs = verdict.audit
        # An audit-log failure never changes the decision (#1108).
        engine.audit_best_effort(getattr(engine, f"log_{event}"))(*args, **kwargs)
    if verdict.notify:
        notify_unattended_block(engine, *verdict.notify)
    for line in verdict.stderr:
        print(line, file=sys.stderr)
    if verdict.decision == "block":
        sys.exit(2)
    if verdict.decision == "ask":
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": _HOOK_EVENT,
            "permissionDecision": "ask",
            "permissionDecisionReason": verdict.reason,
        }}))
    sys.exit(0)
