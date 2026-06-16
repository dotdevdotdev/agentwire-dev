"""CLI for polite agent-to-agent messaging — ``agentwire msg ...``.

``msg`` is the *sibling* of ``send``: ``send`` pastes into the prompt and
presses Enter right now (forceful control); ``msg`` drops a typed message into
the recipient's file inbox and the watchdog injects it only when the input box
is empty and the pane is a safe target — so a worker reporting back never
clobbers a half-typed human draft.

    agentwire msg send --to <session|@all> [--kind note|done|request|escalation] <text>
    agentwire msg inbox [-s <session>]      # peek pending (does not drain)
    agentwire msg dead  [-s <session>]      # list dropped (dead-lettered) msgs
    agentwire msg flush [-s <session>]      # attempt a drain now (still gated)

The drain also rides ``agentwire limits tick`` every 60s, so messages flow
without anyone running ``flush``.
"""

from __future__ import annotations

import json

from . import inbox, pane_manager


def _current_session() -> "str | None":
    return pane_manager.get_current_session()


def cmd_msg_send(args) -> int:
    """Enqueue a message for a session (or @all)."""
    text = " ".join(args.text) if args.text else ""
    if not text.strip():
        print("Usage: agentwire msg send --to <session> <text>", flush=True)
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": "empty message"}))
        return 1

    sender = getattr(args, "from_session", None) or _current_session() or "unknown"
    try:
        written = inbox.enqueue(args.to, text, kind=args.kind, sender=sender)
    except ValueError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return 1

    if not written:
        reason = (
            "@all → no live agent sessions"
            if args.to == inbox.BROADCAST_TOKEN
            else f"no recipients for '{args.to}'"
        )
        if getattr(args, "json", False):
            print(json.dumps({
                "success": True, "queued": [], "recipients": [], "reason": reason,
            }))
        else:
            print(f"Nothing queued — {reason}.")
        return 0

    if getattr(args, "json", False):
        print(json.dumps({
            "success": True,
            "queued": [m.to_dict() for m in written],
            "recipients": [m.to for m in written],
        }))
    else:
        recips = ", ".join(m.to for m in written)
        print(f"Queued {args.kind} from {sender} → {recips}")
    return 0


def cmd_msg_inbox(args) -> int:
    """Peek pending messages without draining."""
    session = getattr(args, "session", None) or _current_session()
    if not session:
        print("No session (use -s or run inside a session)")
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": "no session"}))
        return 1

    messages = inbox.list_messages(session)
    if getattr(args, "json", False):
        print(json.dumps({
            "success": True,
            "session": session,
            "pending": [m.to_dict() for m in messages],
        }))
        return 0

    if not messages:
        print(f"Inbox empty for {session}")
        return 0
    print(f"{len(messages)} pending for {session}:")
    for m in messages:
        print(f"  [{m.kind}] from {m.sender} (attempts={m.attempts}): {m.text}")
    return 0


def _fmt_ts(ms: int) -> str:
    """Epoch-ms → local ``YYYY-MM-DD HH:MM`` (or ``—`` when unset)."""
    if not ms:
        return "—"
    import datetime

    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def cmd_msg_dead(args) -> int:
    """List dead-lettered messages (dropped after MAX_ATTEMPTS retries).

    These are messages a recipient never accepted — its input box stayed busy,
    or it was parked/non-agent the whole time. They are *not* retried; this is
    where silent data loss became visible.
    """
    session = getattr(args, "session", None) or _current_session()
    sessions = [session] if session else inbox.dead_sessions()

    grouped = [(s, inbox.list_dead(s)) for s in sessions]
    grouped = [(s, msgs) for s, msgs in grouped if msgs]
    total = sum(len(msgs) for _, msgs in grouped)

    if getattr(args, "json", False):
        print(json.dumps({
            "success": True,
            "total": total,
            "sessions": [
                {"session": s, "dead": [m.to_dict() for m in msgs]}
                for s, msgs in grouped
            ],
        }))
        return 0

    if not total:
        scope = f" for {session}" if session else ""
        print(f"No dead-lettered messages{scope}.")
        return 0

    print(f"{total} dead-lettered message(s):")
    for s, msgs in grouped:
        print(f"\n{s} ({len(msgs)}):")
        for m in msgs:
            print(
                f"  [{m.kind}] from {m.sender} — died {_fmt_ts(m.dead_ts)} "
                f"after {m.attempts} attempts ({m.reason or 'unknown'})"
            )
            print(f"      {m.text}")
    return 0


def cmd_msg_flush(args) -> int:
    """Attempt a drain now (still gated on an empty box + safe target)."""
    session = getattr(args, "session", None)
    if session:
        result = inbox.flush_session(session)
        payload = {"success": True, **result}
    else:
        result = inbox.tick()
        payload = {"success": True, **result}

    if getattr(args, "json", False):
        print(json.dumps(payload))
        return 0

    if session:
        if result.get("delivered"):
            print(f"Delivered {result['delivered']} to {session}")
        else:
            print(f"Deferred {session}: {result.get('reason')}")
    else:
        flushed = result.get("flushed", [])
        deferred = result.get("deferred", [])
        if result.get("skipped"):
            print(result["skipped"])
        elif not flushed and not deferred:
            print("No pending messages")
        else:
            for r in flushed:
                print(f"delivered {r['delivered']} → {r['session']}")
            for r in deferred:
                print(f"deferred {r['session']}: {r.get('reason')}")
    return 0


def register_msg_parser(subparsers) -> None:
    msg_parser = subparsers.add_parser(
        "msg",
        help="Polite agent-to-agent messaging (file inbox, never clobbers a draft)",
        description=(
            "Drop a typed message into a session's durable inbox; the watchdog "
            "injects it only when the input box is empty and the pane is safe. "
            "The non-interrupting sibling of `agentwire send`. "
            "See docs/wiki/sessions/messaging.md."
        ),
    )
    msg_sub = msg_parser.add_subparsers(dest="msg_command")

    send_parser = msg_sub.add_parser("send", help="Queue a message for a session or @all")
    send_parser.add_argument(
        "--to", required=True, help="Recipient session name, or @all to broadcast"
    )
    send_parser.add_argument(
        "--kind", default="note", choices=inbox.KINDS,
        help="Message kind (default: note)",
    )
    send_parser.add_argument(
        "--from", dest="from_session", default=None,
        help="Override sender (defaults to the current session)",
    )
    send_parser.add_argument("text", nargs="+", help="Message text")
    send_parser.add_argument("--json", action="store_true", help="Output JSON")
    send_parser.set_defaults(func=cmd_msg_send)

    inbox_parser = msg_sub.add_parser("inbox", help="Peek pending messages (no drain)")
    inbox_parser.add_argument("-s", "--session", default=None, help="Session (default: current)")
    inbox_parser.add_argument("--json", action="store_true", help="Output JSON")
    inbox_parser.set_defaults(func=cmd_msg_inbox)

    dead_parser = msg_sub.add_parser(
        "dead", help="List dead-lettered messages (dropped after retries)"
    )
    dead_parser.add_argument(
        "-s", "--session", default=None,
        help="Session (default: current, or all sessions when run outside one)",
    )
    dead_parser.add_argument("--json", action="store_true", help="Output JSON")
    dead_parser.set_defaults(func=cmd_msg_dead)

    flush_parser = msg_sub.add_parser("flush", help="Attempt a drain now (still gated)")
    flush_parser.add_argument(
        "-s", "--session", default=None, help="Session to flush (default: all)"
    )
    flush_parser.add_argument("--json", action="store_true", help="Output JSON")
    flush_parser.set_defaults(func=cmd_msg_flush)

    msg_parser.set_defaults(func=cmd_msg_inbox)
