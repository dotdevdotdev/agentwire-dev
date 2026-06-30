"""CLI for polite agent-to-agent messaging — ``agentwire msg ...``.

``msg`` is the *sibling* of ``send``: ``send`` pastes into the prompt and
presses Enter right now (forceful control); ``msg`` drops a typed message into
the recipient's file inbox and the watchdog injects it only when the input box
is empty and the pane is a safe target — so a worker reporting back never
clobbers a half-typed human draft.

    agentwire msg send --to <session|@all> [--kind note|done|request|escalation|ingest] <text>
    agentwire msg inbox [-s <session>]      # peek pending + passive (does not drain/consume)
    agentwire msg pull  [-s <session>]      # read + REMOVE passive (ingest) messages
    agentwire msg dead  [-s <session>]      # list dropped (dead-lettered) msgs
    agentwire msg dead  --purge [-s <session>] [--older-than 7d]  # clear the graveyard
    agentwire msg flush [-s <session>]      # attempt a drain now (still gated)

The drain also rides ``agentwire limits tick`` every 60s, so messages flow
without anyone running ``flush``.

The ``ingest`` kind is **passive**: never auto-delivered (the watchdog skips
it), so it never drives the recipient. It waits until the recipient pulls it
with ``msg pull`` — the "awareness without being driven" primitive.
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
        written = inbox.enqueue(args.to, text, kind=args.kind, sender=sender,
                                ref=getattr(args, "ref", ""))
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
    passive = inbox.list_ingest(session)
    if getattr(args, "json", False):
        print(json.dumps({
            "success": True,
            "session": session,
            "pending": [m.to_dict() for m in messages],
            "passive": [m.to_dict() for m in passive],
        }))
        return 0

    if not messages and not passive:
        print(f"Inbox empty for {session}")
        return 0
    if messages:
        print(f"{len(messages)} pending for {session}:")
        for m in messages:
            print(f"  [{m.kind}] from {m.sender} (attempts={m.attempts}): {m.text}")
    if passive:
        print(f"{len(passive)} passive (ingest) for {session} — pull to consume:")
        for m in passive:
            print(f"  [{m.kind}] from {m.sender}: {m.text}")
            if m.ref:
                print(f"      ref: {m.ref}")
    return 0


def cmd_msg_pull(args) -> int:
    """Read and REMOVE passive (ingest) messages — the voluntary pull.

    This is how an anchor ingests awareness signals on the human's cue: the
    watchdog never delivers ingest messages, so pulling is the only way they
    leave the inbox. The content they point at lives in files, not the message.
    """
    session = getattr(args, "session", None) or _current_session()
    if not session:
        print("No session (use -s or run inside a session)")
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": "no session"}))
        return 1

    pulled = inbox.pull_ingest(session)
    if getattr(args, "json", False):
        print(json.dumps({
            "success": True,
            "session": session,
            "pulled": [m.to_dict() for m in pulled],
        }))
        return 0

    if not pulled:
        print(f"No passive (ingest) messages for {session}")
        return 0
    print(f"Pulled {len(pulled)} passive message(s) for {session}:")
    for m in pulled:
        print(f"  [{m.kind}] from {m.sender}: {m.text}")
        if m.ref:
            print(f"      ref: {m.ref}")
    return 0


def _fmt_ts(ms: int) -> str:
    """Epoch-ms → local ``YYYY-MM-DD HH:MM`` (or ``—`` when unset)."""
    if not ms:
        return "—"
    import datetime

    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _parse_duration(text: str) -> "int | None":
    """Parse ``7d`` / ``12h`` / ``30m`` / ``45s`` / ``2w`` (or bare seconds) →
    seconds. Returns None for anything unparseable."""
    import re

    m = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", text or "")
    if not m:
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2) or "s"]
    return int(m.group(1)) * mult


def _purge_dead(args) -> int:
    """Clear dead-lettered corpses. ``-s`` scopes to one session; without it the
    whole graveyard is cleared (purge never falls back to the *current* session
    — that's a listing convenience, too sharp an edge for a delete)."""
    import sys
    import time

    session = getattr(args, "session", None)
    before_ms = None
    older = getattr(args, "older_than", None)
    if older:
        secs = _parse_duration(older)
        if secs is None:
            print(f"Invalid --older-than value: {older!r} (use e.g. 7d, 12h, 30m)", file=sys.stderr)
            return 2
        before_ms = int(time.time() * 1000) - secs * 1000

    removed = inbox.purge_dead(session, before_ms=before_ms)
    if getattr(args, "json", False):
        print(json.dumps({
            "success": True, "purged": removed,
            "session": session, "older_than": older,
        }))
        return 0
    scope = f" for {session}" if session else ""
    when = f" older than {older}" if older else ""
    print(f"Purged {removed} dead-lettered message(s){scope}{when}.")
    return 0


def cmd_msg_dead(args) -> int:
    """List (or, with ``--purge``, clear) dead-lettered messages.

    These are messages a recipient never accepted — its input box stayed busy,
    or it was parked/non-agent the whole time. They are *not* retried; this is
    where silent data loss became visible. ``--purge`` is the human/ops cleanup
    for the graveyard `doctor` surfaces.
    """
    if getattr(args, "purge", False):
        return _purge_dead(args)
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


def cmd_msg_purge(args) -> int:
    """Drop a session's pending (undelivered) messages — the self-heal escape hatch."""
    session = getattr(args, "session", None) or _current_session()
    if not session:
        print("No session (use the session name, or run inside one)")
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": "no session"}))
        return 1
    removed = inbox.purge_pending(session)
    if getattr(args, "json", False):
        print(json.dumps({"success": True, "session": session, "purged": removed}))
        return 0
    print(f"Purged {removed} pending message(s) from {session}")
    return 0


def cmd_msg_flush(args) -> int:
    """Attempt a drain now (gated on an empty box + safe target unless --force)."""
    session = getattr(args, "session", None)
    force = getattr(args, "force", False)
    if force and not session:
        msg = "--force requires -s <session> (refuses to force-drain every inbox)"
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": msg}))
        else:
            print(msg)
        return 1
    if session:
        result = inbox.flush_session(session, force=force)
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
    send_parser.add_argument(
        "--ref", default="",
        help="Optional machine-readable pointer (e.g. a report path) — surfaced as a typed field; ideal with --kind ingest",
    )
    send_parser.add_argument("text", nargs="+", help="Message text")
    send_parser.add_argument("--json", action="store_true", help="Output JSON")
    send_parser.set_defaults(func=cmd_msg_send)

    inbox_parser = msg_sub.add_parser("inbox", help="Peek pending + passive messages (no drain/consume)")
    inbox_parser.add_argument("-s", "--session", default=None, help="Session (default: current)")
    inbox_parser.add_argument("--json", action="store_true", help="Output JSON")
    inbox_parser.set_defaults(func=cmd_msg_inbox)

    pull_parser = msg_sub.add_parser("pull", help="Read + remove passive (ingest) messages")
    pull_parser.add_argument("-s", "--session", default=None, help="Session (default: current)")
    pull_parser.add_argument("--json", action="store_true", help="Output JSON")
    pull_parser.set_defaults(func=cmd_msg_pull)

    dead_parser = msg_sub.add_parser(
        "dead", help="List (or --purge) dead-lettered messages (dropped after retries)"
    )
    dead_parser.add_argument(
        "-s", "--session", default=None,
        help="Session (list default: current, or all when outside one; "
             "purge default: all — never the current session)",
    )
    dead_parser.add_argument(
        "--purge", action="store_true",
        help="Delete dead-lettered corpses instead of listing them",
    )
    dead_parser.add_argument(
        "--older-than", dest="older_than", default=None, metavar="DUR",
        help="With --purge: only clear corpses older than DUR (e.g. 7d, 12h, 30m)",
    )
    dead_parser.add_argument("--json", action="store_true", help="Output JSON")
    dead_parser.set_defaults(func=cmd_msg_dead)

    purge_parser = msg_sub.add_parser(
        "purge",
        help="Drop a session's pending messages (self-heal a wedged inbox)",
    )
    purge_parser.add_argument(
        "session", nargs="?", default=None,
        help="Session to purge (default: current)",
    )
    purge_parser.add_argument("--json", action="store_true", help="Output JSON")
    purge_parser.set_defaults(func=cmd_msg_purge)

    flush_parser = msg_sub.add_parser("flush", help="Attempt a drain now (gated unless --force)")
    flush_parser.add_argument(
        "-s", "--session", default=None, help="Session to flush (default: all)"
    )
    flush_parser.add_argument(
        "--force", action="store_true",
        help="Bypass the empty-box gate and paste anyway (requires -s; may land mid-draft)",
    )
    flush_parser.add_argument("--json", action="store_true", help="Output JSON")
    flush_parser.set_defaults(func=cmd_msg_flush)

    msg_parser.set_defaults(func=cmd_msg_inbox)
