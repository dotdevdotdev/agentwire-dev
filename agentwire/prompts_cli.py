"""CLI for prompt routing — ``agentwire prompts ...``.

The sweep rides the usage-limit watchdog (``agentwire limits tick``, every
60s); these commands are the manual/diagnostic surface plus the guarded
answer primitive parents are told to use.
"""

from __future__ import annotations

import json
from datetime import datetime

from . import prompt_router


def _fmt_local(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %I:%M%p")
    except (ValueError, TypeError):
        return str(iso)


def cmd_prompts_tick(args) -> int:
    """Run one prompt sweep now (the watchdog does this every minute)."""
    result = prompt_router.tick()
    if getattr(args, "json", False):
        print(json.dumps(result))
        return 0
    if result.get("skipped"):
        print(result["skipped"])
        return 0
    for entry in result.get("routed", []):
        print(f"routed: {entry['session']}.{entry['pane']} ({entry['kind']}) → {entry['parent']}")
    for entry in result.get("deferred", []):
        print(f"deferred: {entry['session']}.{entry['pane']} ({entry['kind']})")
    for entry in result.get("active", []):
        print(f"active: {entry['session']}.{entry['pane']} ({entry['kind']})")
    if not any(result.get(k) for k in ("routed", "deferred", "active")):
        print("no live prompts")
    return 0


def cmd_prompts_status(args) -> int:
    """Show active prompt markers (prompts awaiting an answer)."""
    markers = prompt_router.list_markers()
    if getattr(args, "json", False):
        print(json.dumps({"markers": markers}, indent=2))
        return 0
    if not markers:
        print("No prompts pending")
        return 0
    for m in markers:
        notified = _fmt_local(m.get("notified_at"))
        print(
            f"{m['session']}.{m['pane']}  kind={m.get('kind')}  "
            f"status={m.get('status')}  parent={m.get('parent') or '-'}  "
            f"hash={m.get('hash')}  notified={notified}"
        )
    return 0


def cmd_prompts_clear(args) -> int:
    """Drop a prompt marker (forces re-detection/re-notification next tick)."""
    session = args.session
    pane = getattr(args, "pane", 0) or 0
    prompt_router.clear_marker(session, pane)
    if not getattr(args, "json", False):
        print(f"Cleared marker for {session}.{pane}")
    else:
        print(json.dumps({"success": True, "session": session, "pane": pane}))
    return 0


def cmd_prompts_answer(args) -> int:
    """Answer a routed prompt — only if the same prompt is still live.

    Re-captures the pane, re-detects the prompt, and compares the content
    hash from the notification (--expect) before sending any key. A human
    may have answered first via the portal: first answer wins, this no-ops.
    """
    session = args.session
    pane = getattr(args, "pane", 0) or 0
    keys = args.keys
    ok, message = prompt_router.answer(session, pane, args.expect, keys)
    if getattr(args, "json", False):
        print(json.dumps({"success": ok, "message": message}))
    else:
        print(message)
    return 0 if ok else 1


def register_prompts_parser(subparsers) -> None:
    prompts_parser = subparsers.add_parser(
        "prompts", help="Interactive-prompt routing (worker prompts → parent session)"
    )
    prompts_sub = prompts_parser.add_subparsers(dest="prompts_cmd")

    tick_parser = prompts_sub.add_parser("tick", help="Run one prompt sweep now")
    tick_parser.add_argument("--json", action="store_true", help="Output JSON")
    tick_parser.set_defaults(func=cmd_prompts_tick)

    status_parser = prompts_sub.add_parser("status", help="Show pending prompt markers")
    status_parser.add_argument("--json", action="store_true", help="Output JSON")
    status_parser.set_defaults(func=cmd_prompts_status)

    clear_parser = prompts_sub.add_parser("clear", help="Drop a prompt marker")
    clear_parser.add_argument("-s", "--session", required=True, help="Session name")
    clear_parser.add_argument("--pane", type=int, default=0, help="Pane index (default 0)")
    clear_parser.add_argument("--json", action="store_true", help="Output JSON")
    clear_parser.set_defaults(func=cmd_prompts_clear)

    answer_parser = prompts_sub.add_parser(
        "answer", help="Answer a routed prompt (guarded: verifies the prompt is still live)"
    )
    answer_parser.add_argument("-s", "--session", required=True, help="Session name")
    answer_parser.add_argument("--pane", type=int, default=0, help="Pane index (default 0)")
    answer_parser.add_argument("--expect", required=True,
                               help="Content hash from the [PROMPT ...] notification")
    answer_parser.add_argument("keys", nargs="+",
                               help="tmux keys to send (e.g. 2, or: 1 Enter)")
    answer_parser.add_argument("--json", action="store_true", help="Output JSON")
    answer_parser.set_defaults(func=cmd_prompts_answer)

    prompts_parser.set_defaults(func=cmd_prompts_status)
