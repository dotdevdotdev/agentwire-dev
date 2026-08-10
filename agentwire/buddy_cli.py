"""``agentwire buddy`` — the EXPERIMENTAL voice buddy (spike, branch-only).

Every subcommand here except ``serve`` is read-only with respect to the fleet,
and for ``call`` that is a property of the WIRING rather than of the buddy: it
dispatches with no confirm spine, so a write tool is refused outright instead of
degrading to an ungated write. ``serve`` starts the bridge, and the bridge HOLDS
a spine — so the browser client it serves can reach the one gated write (``msg
send`` to a live session). The only things this module itself writes are the
buddy's identity record, its inbox spool cursor, and (for ``serve``) a localhost
socket.

See ``docs/wiki/voice-layer.md`` for the design and the harness boundary.
"""

from __future__ import annotations

import json
import sys

from . import fleet_alerts
from .voice_layer import delivery, identity, realtime, tools


def _emit(payload: dict, json_mode: bool, lines: "list[str] | None" = None) -> int:
    if json_mode:
        print(json.dumps(payload, indent=2))
    else:
        for line in lines or []:
            print(line)
    return 0 if payload.get("success", True) else 1


def _fail(message: str, json_mode: bool) -> int:
    if json_mode:
        print(json.dumps({"success": False, "error": message}))
    else:
        print(message, file=sys.stderr)
    return 1


def cmd_buddy_register(args) -> int:
    json_mode = getattr(args, "json", False)
    try:
        metadata = identity.register(args.name, model=args.model, voice=args.voice)
    except identity.BuddyError as exc:
        return _fail(str(exc), json_mode)
    status = identity.status(args.name)
    return _emit(
        {"success": True, "buddy": status, "metadata": metadata},
        json_mode,
        [
            f"Registered voice buddy '{args.name}'.",
            f"  inbox:  {status['inbox_dir']}",
            f"  spool:  {status['spool_path']}",
            "",
            f"Other sessions can now reach it: agentwire msg send --to {args.name} ...",
            "It has no tmux session — its mail is spooled, not pasted.",
        ],
    )


def cmd_buddy_status(args) -> int:
    json_mode = getattr(args, "json", False)
    try:
        status = identity.status(args.name)
    except identity.BuddyError as exc:
        return _fail(str(exc), json_mode)
    if not status["registered"]:
        return _fail(
            f"No voice buddy named '{args.name}' — register it with "
            f"`agentwire buddy register {args.name}`.",
            json_mode,
        )
    return _emit(
        {"success": True, **status},
        json_mode,
        [
            f"buddy '{status['name']}'",
            f"  role:        {status['role']} (not in the fleet topology)",
            f"  delivery:    {status['delivery']} (spooled, no tmux pane)",
            f"  registered:  {status['registered_at']}",
            f"  pending:     {status['pending']} message(s) awaiting the next drain",
            f"  unread:      {status['unread']} spooled message(s)",
        ],
    )


def cmd_buddy_list(args) -> int:
    json_mode = getattr(args, "json", False)
    found = identity.list_buddies()
    return _emit(
        {"success": True, "buddies": found, "count": len(found)},
        json_mode,
        [f"{b['name']}  unread={b['unread']}  pending={b['pending']}" for b in found]
        or ["No voice buddies registered."],
    )


def cmd_buddy_unregister(args) -> int:
    json_mode = getattr(args, "json", False)
    try:
        removed = identity.unregister(args.name, purge=args.purge)
    except identity.BuddyError as exc:
        return _fail(str(exc), json_mode)
    return _emit(
        {"success": True, "removed": removed},
        json_mode,
        [
            f"Removed voice buddy '{args.name}'.",
            (
                f"  purged spool/cursor and {removed['pending']} pending message(s)"
                if args.purge
                else "  spool and pending mail left on disk (use --purge to drop them)"
            ),
        ],
    )


def cmd_buddy_inbox(args) -> int:
    """What other sessions have reported to the buddy."""
    json_mode = getattr(args, "json", False)
    if not identity.is_registered(args.name):
        return _fail(f"No voice buddy named '{args.name}'.", json_mode)
    messages = delivery.read_spool(
        args.name, unread_only=not args.all, ack=args.ack
    )
    lines = [
        f"[{m.get('kind')}] from {m.get('from')}: {m.get('text')}" for m in messages
    ] or ["Nothing new."]
    return _emit(
        {"success": True, "count": len(messages), "messages": messages}, json_mode, lines
    )


def cmd_buddy_tools(args) -> int:
    """The tool surface handed to the realtime model."""
    json_mode = getattr(args, "json", False)
    defs = tools.realtime_tool_defs()
    return _emit(
        {"success": True, "count": len(defs), "tools": defs},
        json_mode,
        [f"{d['name']}  —  {d['description']}" for d in defs],
    )


def cmd_buddy_call(args) -> int:
    """Run one tool exactly as the voice layer would — no microphone needed.

    This is how the tool surface is exercised and reviewed: the dispatch path
    the model reaches is the dispatch path this runs.
    """
    json_mode = getattr(args, "json", False)
    call_args = {}
    for pair in args.arg or []:
        if "=" not in pair:
            return _fail(f"--arg expects key=value, got {pair!r}", json_mode)
        key, _, value = pair.partition("=")
        try:
            call_args[key] = json.loads(value)
        except json.JSONDecodeError:
            call_args[key] = value
    result = tools.dispatch(args.tool, call_args, args.name)
    return _emit(result, json_mode, [json.dumps(result, indent=2)])


def cmd_buddy_mint(args) -> int:
    """Mint an ephemeral Realtime session (prints the client secret)."""
    json_mode = getattr(args, "json", False)
    from .voice_layer import instructions as buddy_instructions

    if not identity.is_registered(args.name):
        return _fail(f"No voice buddy named '{args.name}'.", json_mode)
    try:
        session = realtime.mint_session(
            instructions=buddy_instructions.build_instructions(),
            tools=tools.realtime_tool_defs(),
            model=args.model or realtime.DEFAULT_MODEL,
            voice=args.voice or realtime.DEFAULT_VOICE,
        )
    except realtime.RealtimeError as exc:
        return _fail(str(exc), json_mode)
    return _emit(
        {"success": True, **session},
        json_mode,
        [
            f"session:    {session['id']}",
            f"model:      {session['model']}",
            f"expires_at: {session['expires_at']}",
            "(client secret withheld from human output — use --json)",
        ],
    )


def cmd_buddy_serve(args) -> int:
    """Serve the browser client on localhost until interrupted."""
    from .voice_layer import server

    if not identity.is_registered(args.name):
        return _fail(f"No voice buddy named '{args.name}'.", getattr(args, "json", False))
    # Renew the fleet-alert lease (#982) — a buddy being started is a buddy that
    # wants the fleet's escalations, however long ago it was registered.
    fleet_alerts.subscribe(args.name)
    httpd, url = server.serve(
        args.name, port=args.port, model=args.model, voice=args.voice
    )
    print(f"buddy '{args.name}' listening on {url}")
    print("Open that URL in a browser and click Start talking. Ctrl-C to stop.")
    try:
        while True:
            httpd._BaseServer__is_shut_down.wait(1)  # noqa: SLF001  (stdlib event)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


def register_buddy_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "buddy",
        help="EXPERIMENTAL: realtime voice buddy over the fleet (spike)",
        description=(
            "A realtime voice layer the owner talks to about the fleet. It is not a "
            "coding harness: it never writes code, never owns a worktree, and never "
            "appears in the topology. It reads the fleet and its own mail, and has "
            "exactly one write: passing a message to a session that is already "
            "running, gated by a spoken confirm phrase."
        ),
    )
    sub = parser.add_subparsers(dest="buddy_command", required=True)

    def _common(p, *, name_default=identity.DEFAULT_NAME):
        p.add_argument("name", nargs="?", default=name_default, help="Buddy name")
        p.add_argument("--json", action="store_true", help="Output JSON")
        return p

    reg = _common(sub.add_parser("register", help="Create the buddy's session identity"))
    reg.add_argument("--model", default="", help=f"Realtime model (default: {realtime.DEFAULT_MODEL})")
    reg.add_argument("--voice", default="", help=f"Realtime voice (default: {realtime.DEFAULT_VOICE})")
    reg.set_defaults(func=cmd_buddy_register)

    _common(sub.add_parser("status", help="Show the buddy's identity and mail counts")).set_defaults(
        func=cmd_buddy_status
    )

    lst = sub.add_parser("list", help="List registered buddies")
    lst.add_argument("--json", action="store_true", help="Output JSON")
    lst.set_defaults(func=cmd_buddy_list)

    unreg = _common(sub.add_parser("unregister", help="Remove the buddy's identity"))
    unreg.add_argument("--purge", action="store_true",
                       help="Also drop the spool, cursor and any pending mail")
    unreg.set_defaults(func=cmd_buddy_unregister)

    box = _common(sub.add_parser("inbox", help="Read mail other sessions sent the buddy"))
    box.add_argument("--all", action="store_true", help="Include already-read messages")
    box.add_argument("--ack", action="store_true", help="Mark the returned messages read")
    box.set_defaults(func=cmd_buddy_inbox)

    tl = sub.add_parser("tools", help="Show the tool surface handed to the model")
    tl.add_argument("--json", action="store_true", help="Output JSON")
    tl.set_defaults(func=cmd_buddy_tools)

    call = _common(sub.add_parser("call", help="Run one tool without a microphone"))
    call.add_argument("tool", help="Tool name (see `agentwire buddy tools`)")
    call.add_argument("--arg", action="append", help="Tool argument as key=value (repeatable)")
    call.set_defaults(func=cmd_buddy_call)

    mint = _common(sub.add_parser("mint", help="Mint an ephemeral Realtime session"))
    mint.add_argument("--model", default="")
    mint.add_argument("--voice", default="")
    mint.set_defaults(func=cmd_buddy_mint)

    srv = _common(sub.add_parser("serve", help="Serve the browser client on localhost"))
    srv.add_argument("--port", type=int, default=8788,
                     help="Port on 127.0.0.1 (default: 8788 — never a portal port)")
    srv.add_argument("--model", default="")
    srv.add_argument("--voice", default="")
    srv.set_defaults(func=cmd_buddy_serve)
