"""Claude Code adapter (#1209 M3/M4): hook payload -> ``ToolCall``/``Context``, ``Verdict`` -> exit code.

The five PreToolUse hooks differ only in WHICH tools they handle and how their rule
sources load; every one is ``parse -> decide -> render``, and this file holds the parse
and render halves (``policy.Guard`` is the decide half) plus ``run_hook``, the entry
``hook.py`` calls. A hook kind declares a tool table ``{tool_name: (kind, (payload
fields...))}``.

``render`` is the only place a decision becomes an exit code, a stderr line, an audit row
and a notification, so a verdict that reaches it can only be rendered one way.

Standard library only.
"""

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from damage_control import audit, engine
from damage_control.policy import BYPASS, Context, Guard, ToolCall
from damage_control.profile import load_profile

# ``permission_mode`` values that mean the human opted out of ask-escalation friction:
#   - "bypassPermissions" -> --dangerously-skip-permissions
#   - "auto"              -> autonomous /loop or similar auto mode
# Hard blocks still fire either way; an ``ask`` becomes an allow — EXCEPT an unverifiable one.
BYPASS_PERMISSION_MODES = frozenset({"bypassPermissions", "auto"})

# stdout of an ask: Claude Code's PreToolUse permission-decision envelope.
_HOOK_EVENT = "PreToolUse"

_Tools = Optional[Dict[str, Tuple[str, Tuple[str, ...]]]]

# hook kind -> (tool table, whether it judges shell commands and so loads the tooldefs).
# ``None`` for a table means every tool is an ``mcp`` call; the decision itself lives in
# ``policy.Guard``.
HOOK_KINDS: Dict[str, Tuple[_Tools, bool]] = {
    # Bash and PowerShell calls: judge the command line.
    "bash": ({"Bash": ("shell", ("command",)), "PowerShell": ("shell", ("command",))}, True),
    # Edit and NotebookEdit calls: judge the target path. NotebookEdit writes files exactly
    # like Edit but names its target ``notebook_path``: an operation refused via Edit must
    # not be permitted via NotebookEdit (#923).
    "edit": ({"Edit": ("edit", ("file_path", "notebook_path")),
              "NotebookEdit": ("edit", ("file_path", "notebook_path"))}, False),
    "write": ({"Write": ("write", ("file_path",))}, False),
    # Read, Grep and Glob: judge the path they read or list. ``Glob`` only reveals filenames,
    # but a zero-access directory is still off-limits.
    "read": ({"Read": ("read", ("file_path",)), "Grep": ("read", ("path",)),
              "Glob": ("read", ("path",))}, False),
    "mcp": (None, True),
}


class HookUsageError(ValueError):
    """The hook was invoked with arguments it does not understand. Blocks, like any hook failure."""


def parse_args(argv: List[str]) -> Tuple[str, Optional[str]]:
    """``(hook kind, profile manifest path | None)`` from the hook's command-line arguments."""
    args = list(argv)
    profile: Optional[str] = None
    if "--profile" in args:
        i = args.index("--profile")
        if i + 1 >= len(args):
            raise HookUsageError("--profile needs a path")
        profile = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1 or args[0] not in HOOK_KINDS:
        raise HookUsageError(f"usage: hook {{{'|'.join(HOOK_KINDS)}}} [--profile PATH]; got {argv!r}")
    return args[0], profile


def hook_namespace() -> Any:
    """What ``Guard`` and ``render`` reach engine functions and audit writers through."""
    names = {n: getattr(audit, n) for n in ("log_allowed", "log_asked", "log_blocked", "log_disabled", "log_escape")}
    return types.SimpleNamespace(**{**vars(engine), **names})


def parse_payload(payload: Dict[str, Any], tools: Optional[Dict[str, Tuple[str, Tuple[str, ...]]]],
                  engine: Any) -> Tuple[Any, Any]:
    """``(ToolCall | None, Context)``; no call means this hook has nothing to judge.

    ``tools`` maps a tool name to ``(kind, fields)``: for ``shell`` the field names the
    command, for read/write/edit the target path (the first non-empty field wins). ``None``
    means every tool is an ``mcp`` call.
    """
    tool_call, context, bypass = ToolCall, Context, BYPASS
    # Session attribution for audit rows (#940): hand the parsed payload to the audit writers
    # so every log_* call carries the conversation id.
    audit.HOOK_INPUT = payload

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    ctx = context(
        mode=bypass if payload.get("permission_mode", "") in BYPASS_PERMISSION_MODES else "default",
        unattended=engine.is_unattended(),
    )
    if tools is None:
        return tool_call("mcp", tool_name, tool_input=tool_input or {}), ctx
    if tool_name not in tools:
        return None, ctx
    kind, fields = tools[tool_name]
    value = next((tool_input.get(f, "") for f in fields if tool_input.get(f, "")), "")
    if not value:
        return None, ctx
    if kind == "shell":
        return tool_call(kind, tool_name, command=value, cwd=payload.get("cwd")), ctx
    return tool_call(kind, tool_name, paths=(value,)), ctx


def notify_unattended_block(engine: Any, subject: str, reason: str, rule_id: Optional[str]) -> None:
    """Fire-and-forget: tell the owner an unattended action was blocked.

    Runs the profile's notifier argv, detached and non-blocking so it never sits on the
    hook's hot path, with the event as one JSON object on its stdin:
    ``{"event": "unattended_block", "reason": ..., "rule_id": ..., "command": ...}``
    (``command`` is truncated to 500 characters). Failures are swallowed (the block has
    already fired and is audit-logged regardless). A hook never imports the host, hence the
    subprocess.
    """
    argv = engine.PROFILE.unattended_block_notifier
    if not argv:
        return
    event = {"event": "unattended_block", "reason": reason or "", "rule_id": rule_id or "", "command": subject[:500]}
    try:
        child = subprocess.Popen(
            list(argv), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.PIPE,
            start_new_session=True,
        )
        child.stdin.write(json.dumps(event).encode())
        child.stdin.close()
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


def run_hook(argv: List[str]) -> None:
    """The hook entry: parse the arguments, install the profile, judge one payload, exit.

    Everything runs under ``run_hook_guarded``, so any failure here (a bad argument, an
    unreadable profile, a crashed rule load) is a blocking exit 2, never Python's own exit 1.
    """

    def main() -> None:
        kind, profile_path = parse_args(argv)
        if profile_path:
            engine.install_profile(load_profile(Path(profile_path)))
        namespace = hook_namespace()
        tools, tooldefs = HOOK_KINDS[kind]
        guard = Guard(namespace.load_effective_config(tooldefs), namespace)
        call, ctx = parse_payload(namespace.read_hook_input(), tools, namespace)
        if call is None:
            sys.exit(0)
        render(guard.decide(call, ctx), namespace)

    engine.run_hook_guarded(main)
