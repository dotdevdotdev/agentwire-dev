"""``agentwire buddy`` — the voice buddy (BETA, gated on ``beta.voice_layer``).

Every subcommand refuses while the flag is off, naming both prerequisites (the
config key and the secret) — see :func:`_refuse_beta`.

Every subcommand here except ``serve`` is read-only with respect to the fleet,
and for ``call`` that is a property of the WIRING rather than of the buddy: it
dispatches with no confirm spine, so a write tool is refused outright instead of
degrading to an ungated write. ``serve`` starts the bridge, and the bridge HOLDS
a spine — so the browser client it serves can reach the one gated write (``msg
send`` to a live session). The only things this module itself writes are the
buddy's identity record, its inbox spool cursor, (for ``serve``) a localhost
socket, and (for the human-only ``service install|uninstall``) its one
``services.custom`` entry in ``config.yaml``.

See ``docs/wiki/voice-layer.md`` for the design and the harness boundary.
"""

from __future__ import annotations

import functools
import json
import sys

from . import fleet_alerts
from .voice_layer import delivery, identity, realtime, tools

#: The config key that turns this whole command group on. Named in every
#: refusal — a refusal that does not carry the next move is the failure this
#: project keeps closing, and here the owner is often not even at a screen.
BETA_KEY = "beta.voice_layer"

#: The one ``--voice`` help string, shared by register/mint/serve (#1017).
#:
#: Enumerated here rather than declared as argparse ``choices=``, deliberately.
#: ``choices`` does print the set and does reject early — but its rejection is
#: argparse's own ("invalid choice"), which cannot say what the default is, is
#: case-SENSITIVE (so a spoken-then-typed "Cedar" would be refused for no
#: reason), and exits before any handler runs, which means the ``--json``
#: caller gets a parser usage dump on stderr instead of a JSON error object.
#: :func:`_check_voice` does all three properly, and this string is what makes
#: the list discoverable without reaching the error at all.
#:
#: No percent signs anywhere in it: argparse runs help through printf-style
#: interpolation, and a lone one raises at parse time.
_VOICE_HELP = (
    "Realtime voice, one of: " + realtime.voice_list()
    + f" (default: {realtime.DEFAULT_VOICE}; cedar and marin are the newer, "
    "more natural pair). Fixed for a session — switch it live from the picker "
    "on the buddy page."
)


def _refuse_beta(json_mode: bool) -> int:
    """Refuse a buddy command because the beta flag is off.

    Names BOTH prerequisites, deliberately, even though only the flag was
    checked: the flag alone gets you a bridge that mints nothing, and finding
    that out one command later is a second dead end. The secret's location is
    the one blessed spot (``docs/wiki/security/secrets.md``), never a value
    this process reads or echoes.
    """
    if json_mode:
        print(json.dumps({
            "success": False,
            "error": (
                f"The voice buddy is a beta feature and is off by default. "
                f"Set {BETA_KEY}: true in ~/.agentwire/config.yaml, and put "
                f"{realtime.API_KEY_ENV} in ~/.agentwire/.env."
            ),
            "beta_flag": BETA_KEY,
            "config_path": "~/.agentwire/config.yaml",
            "secret": realtime.API_KEY_ENV,
            "secrets_path": "~/.agentwire/.env",
            "docs": "docs/wiki/voice-layer.md",
        }, indent=2))
        return 1
    for line in [
        "The voice buddy is a beta feature and is OFF by default.",
        "",
        "Two things turn it on, and it needs both:",
        "",
        "  1. Opt in — ~/.agentwire/config.yaml:",
        "",
        "       beta:",
        "         voice_layer: true",
        "",
        "  2. Provide the key — ~/.agentwire/.env (chmod 600), the one",
        "     blessed spot for secrets:",
        "",
        f"       {realtime.API_KEY_ENV}=sk-...",
        "",
        "Docs: docs/wiki/voice-layer.md",
    ]:
        print(line, file=sys.stderr)
    return 1


def beta_gated(fn):
    """Wrap a buddy subcommand so it refuses while ``beta.voice_layer`` is off.

    Applied per subcommand rather than once at the parser, because argparse
    dispatches through ``args.func`` and a group-level check would be invisible
    to anything calling a handler directly. The omission that shape invites is
    pinned by a test walking every registered subcommand for ``_beta_gated``.
    """
    @functools.wraps(fn)
    def wrapper(args):
        from .config import enabled_beta_flags

        if "voice_layer" not in enabled_beta_flags():
            return _refuse_beta(getattr(args, "json", False))
        return fn(args)

    wrapper._beta_gated = True
    return wrapper


def _no_buddy(name: str) -> str:
    """The one wording for "that buddy isn't registered", with the next move.

    Four call sites said this and only ``status`` named the fix, so the same
    dead end read as actionable or not depending on which verb you happened to
    reach it through. Same standard as the beta refusal above: a refusal that
    does not carry the next move is the defect this project keeps closing, and
    in this channel the user is often not looking at a screen at all.
    """
    return (
        f"No voice buddy named '{name}' — register it with "
        f"`agentwire buddy register {name}`."
    )


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


def _check_voice(args) -> "int | None":
    """Refuse an unknown ``--voice`` before anything is written or spent.

    Early, and in every verb that takes one: the alternative is a bridge that
    starts, connects, and then produces no audio at all — which in a screenless
    channel is indistinguishable from a mic problem, an API outage, or the
    buddy simply having nothing to say. The refusal carries the whole list,
    because ten closed values are short enough to print and guessing is what
    put the typo there.

    Returns an exit code to propagate, or ``None`` when the voice is fine.
    """
    voice = getattr(args, "voice", "")
    if not voice:
        return None
    try:
        args.voice = realtime.validate_voice(voice)
    except realtime.RealtimeError as exc:
        return _fail(str(exc), getattr(args, "json", False))
    return None


def cmd_buddy_register(args) -> int:
    json_mode = getattr(args, "json", False)
    refused = _check_voice(args)
    if refused is not None:
        return refused
    # Read BEFORE the write: what changed is only knowable against the record
    # that was there. Re-announcing a full registration over a one-word voice
    # change reads like a second identity was created (#1017).
    try:
        delta = identity.registration_delta(
            args.name, model=args.model, voice=args.voice
        )
        metadata = identity.register(args.name, model=args.model, voice=args.voice)
    except identity.BuddyError as exc:
        return _fail(str(exc), json_mode)
    status = identity.status(args.name)
    changes = delta["changes"]
    payload = {
        "success": True,
        "created": not delta["registered"],
        "changes": changes,
        "buddy": status,
        "metadata": metadata,
    }
    if delta["registered"]:
        lines = [
            f"{field} → {change['to']}"
            + (f" (was {change['from']})" if change["from"] else "")
            for field, change in sorted(changes.items())
        ]
        return _emit(
            payload,
            json_mode,
            [f"Voice buddy '{args.name}' is already registered."]
            + [f"  updated {line}" for line in lines]
            + ([] if lines else ["  nothing to change."]),
        )
    return _emit(
        payload,
        json_mode,
        [
            f"Registered voice buddy '{args.name}'.",
            f"  inbox:  {status['inbox_dir']}",
            f"  spool:  {status['spool_path']}",
            f"  voice:  {status['realtime_voice'] or realtime.DEFAULT_VOICE} (default)"
            if not status["realtime_voice"]
            else f"  voice:  {status['realtime_voice']}",
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
        return _fail(_no_buddy(args.name), json_mode)
    return _emit(
        {"success": True, **status},
        json_mode,
        [
            f"buddy '{status['name']}'",
            f"  role:        {status['role']} (not in the fleet topology)",
            f"  delivery:    {status['delivery']} (spooled, no tmux pane)",
            f"  registered:  {status['registered_at']}",
            f"  voice:       {status['realtime_voice'] or realtime.DEFAULT_VOICE}"
            + ("" if status["realtime_voice"] else " (default)"),
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
        return _fail(_no_buddy(args.name), json_mode)
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

    refused = _check_voice(args)
    if refused is not None:
        return refused
    if not identity.is_registered(args.name):
        return _fail(_no_buddy(args.name), json_mode)
    try:
        session = realtime.mint_session(
            instructions=buddy_instructions.build_instructions(),
            tools=tools.realtime_tool_defs(),
            model=args.model or realtime.DEFAULT_MODEL,
            # Explicit flag → the buddy's recorded voice → the default.
            voice=identity.resolve_voice(args.name, args.voice),
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

    refused = _check_voice(args)
    if refused is not None:
        return refused
    if not identity.is_registered(args.name):
        return _fail(_no_buddy(args.name), getattr(args, "json", False))
    # Renew the fleet-alert lease (#982) — a buddy being started is a buddy that
    # wants the fleet's escalations, however long ago it was registered. Guarded:
    # a failure here costs alerts, and a bridge that refuses to serve because the
    # ALERTING subsystem is unhappy is a strictly worse outcome than a quiet one.
    try:
        fleet_alerts.subscribe(args.name)
    except Exception as exc:  # noqa: BLE001  # optional extra, never fatal
        fleet_alerts.log_event("subscribe_failed", session=args.name, error=str(exc))
    httpd, url = server.serve(
        args.name, port=args.port, model=args.model, voice=args.voice
    )
    print(f"buddy '{args.name}' listening on {url}")
    print(f"voice: {identity.resolve_voice(args.name, args.voice)} "
          f"(switch it on the page — no restart needed)")
    print(f"trace: {httpd.bridge.trace.path}  (agentwire buddy log --tail)")
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


def cmd_buddy_log(args) -> int:
    """Read the bridge's per-run traces (#1191) — diagnosis without the owner."""
    import time

    from .voice_layer import trace

    json_mode = getattr(args, "json", False)
    if args.list:
        runs = [
            {"run": r.stem, "path": str(r), "bytes": r.stat().st_size}
            for r in trace.list_runs()
        ]
        return _emit({"success": True, "runs": runs}, json_mode,
                     [f"{r['run']}  {r['bytes']:>9} B" for r in runs] or ["no buddy runs traced yet"])

    if args.stalls and args.run is None:
        paths = trace.list_runs()
    else:
        path = trace.resolve_run(args.run or "latest")
        if path is None:
            return _fail(f"no buddy run matching {args.run or 'latest'!r} "
                         f"under {trace.TRACE_DIR}", json_mode)
        paths = [path]

    events = [e for p in reversed(paths) for e in trace.read_events(p)]
    if args.stalls:
        events = [e for e in events if e.get("kind") in trace.STALL_KINDS]
    if args.lines and not args.stalls:
        events = events[-args.lines:]
    if json_mode and not args.tail:
        return _emit({"success": True, "runs": [p.stem for p in paths], "events": events}, True)
    for event in events:
        print(json.dumps(event) if json_mode else trace.format_event(event))
    if not args.tail:
        if not events:
            print("no stalls recorded" if args.stalls else "run is empty")
        return 0
    path = paths[0]
    with path.open(encoding="utf-8") as fh:
        fh.seek(0, 2)
        try:
            while True:
                line = fh.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if args.stalls and event.get("kind") not in trace.STALL_KINDS:
                    continue
                print(json.dumps(event) if json_mode else trace.format_event(event), flush=True)
        except KeyboardInterrupt:
            return 0


# ─────────────────────────────────────────────────────────────
# Supervised service (#1184)
# ─────────────────────────────────────────────────────────────
#
# #983 gave the services registry a generic `command` kind, and the buddy is
# one caller of it — but the entry lives in ~/.agentwire/config.yaml, which is
# protected control plane, and `command` is execution plane, so `config set`
# refuses it by design. Pasting YAML from the wiki was the only way in, which
# in practice meant no machine had one. These verbs write that one FIXED entry
# (no free-form command text) and are human-only, the same gate as
# `safety packs enable`: a tty check here plus a PROTECTED_COMMAND_PATTERNS
# entry that stops an agent's Bash tool before it gets this far.

#: The service's name — already in ``services._BUILTIN_SERVICE_SESSIONS``, so
#: it is classified as infrastructure everywhere the fleet is listed. Not the
#: buddy's own name: ``buddy`` is a session identity other sessions message,
#: and a tmux session under that name would be a pane `send` could paste into.
SERVICE_NAME = "agentwire-buddy"
DEFAULT_PORT = 8788
_SERVE_COMMAND = "agentwire buddy serve"


def service_name(name: str) -> str:
    return SERVICE_NAME if name == identity.DEFAULT_NAME else f"{SERVICE_NAME}-{name}"


def buddy_service_configured(custom_services) -> bool:
    """Does any ``services.custom`` entry supervise a buddy bridge?

    Keyed on the command, not the name, so a hand-pasted wiki entry under
    another name counts too.
    """
    return any((s.command or "").startswith(_SERVE_COMMAND) for s in custom_services)


def service_entry(name: str, *, port: int, autostart: bool) -> dict:
    """The ``services.custom`` entry that supervises ``buddy serve <name>``.

    *name* must already have passed ``identity.validate_name`` — that charset is
    what makes interpolating it into a shell command safe.
    """
    return {
        "name": service_name(name),
        "command": f"{_SERVE_COMMAND} {name} --port {int(port)}",
        "autostart": bool(autostart),
        "restart": "on-failure",
        # tmux_session, not an HTTP probe: `/` hands over the run token
        # (docs/wiki/voice-layer.md §6).
        "healthcheck": {"kind": "tmux_session", "interval": 60},
    }


def upsert_service_entry(raw: dict, entry: dict) -> str:
    """Put *entry* into ``raw['services']['custom']``; say what happened."""
    services = raw.setdefault("services", {})
    if not isinstance(services, dict):
        raise ValueError("services: in config.yaml is not a mapping")
    custom = services.get("custom") or []
    for i, existing in enumerate(custom):
        if isinstance(existing, dict) and existing.get("name") == entry["name"]:
            if existing == entry:
                return "unchanged"
            custom[i] = entry
            services["custom"] = custom
            return "updated"
    services["custom"] = [*custom, entry]
    return "installed"


def remove_service_entry(raw: dict, svc_name: str) -> bool:
    custom = (raw.get("services") or {}).get("custom") or []
    kept = [e for e in custom if not (isinstance(e, dict) and e.get("name") == svc_name)]
    if len(kept) == len(custom):
        return False
    raw["services"]["custom"] = kept
    return True


def _host_gate(verb: str, json_mode: bool) -> "int | None":
    from .tasks_cli import _host_context_ok

    if _host_context_ok():
        return None
    return _fail(
        f"agentwire buddy service {verb} writes ~/.agentwire/config.yaml (protected "
        "control plane) — human-only. Run it from your own terminal, not through an agent.",
        json_mode,
    )


def _registered_service(svc_name: str):
    from . import services as services_mod
    from .config import load_config

    return next((s for s in services_mod.registry(load_config()) if s.name == svc_name), None)


def cmd_buddy_service_install(args) -> int:
    """Register the buddy if needed, write its service entry, start it."""
    import os

    from . import services as services_mod
    from .config_cli import _load_raw, _save_raw
    from .env_loader import missing_env_message

    json_mode = getattr(args, "json", False)
    refused = _host_gate("install", json_mode)
    if refused is not None:
        return refused
    try:
        identity.validate_name(args.name)
        created = not identity.is_registered(args.name)
        if created:
            identity.register(args.name)
    except identity.BuddyError as exc:
        return _fail(str(exc), json_mode)

    entry = service_entry(args.name, port=args.port, autostart=args.autostart)
    raw = _load_raw()
    try:
        outcome = upsert_service_entry(raw, entry)
    except ValueError as exc:
        return _fail(f"Refusing to write: {exc}. Fix the file by hand first.", json_mode)
    if outcome != "unchanged":
        _save_raw(raw)

    key_problem = (None if os.environ.get(realtime.API_KEY_ENV, "").strip()
                   else missing_env_message(realtime.API_KEY_ENV))
    started, start_msg = None, "not started (--no-start)"
    if not args.no_start:
        services_mod.set_disabled(entry["name"], False)
        svc = _registered_service(entry["name"])
        started, start_msg = services_mod.start_service(svc)

    url = f"http://127.0.0.1:{int(args.port)}/"
    payload = {
        "success": started is not False,
        "service": entry["name"], "entry": outcome, "buddy_registered": created,
        "autostart": entry["autostart"], "started": started, "result": start_msg,
        "url": url, "key_problem": key_problem,
    }
    lines = [
        f"Service {entry['name']}: {outcome} in ~/.agentwire/config.yaml"
        + (f" (registered buddy '{args.name}')" if created else ""),
        f"  start:     {start_msg}",
        "  autostart: " + ("on — boots with the portal" if entry["autostart"]
                           else "off — rerun with --autostart to boot it with the portal"),
    ]
    if key_problem:
        lines += ["", f"  [!!] {key_problem}"]
    if started:
        lines += [
            "",
            f"Open {url} and click Start talking — the greeting is the liveness probe.",
            f"Restart: agentwire services down {entry['name']} && agentwire services up "
            f"{entry['name']}, then reload the page (each run mints a new token).",
        ]
    if not json_mode and started is False:
        for line in lines:
            print(line, file=sys.stderr)
        return 1
    return _emit(payload, json_mode, lines)


def cmd_buddy_service_uninstall(args) -> int:
    """Stop the buddy's service and remove its entry. The identity stays."""
    from . import services as services_mod
    from .config_cli import _load_raw, _save_raw

    json_mode = getattr(args, "json", False)
    refused = _host_gate("uninstall", json_mode)
    if refused is not None:
        return refused
    svc_name = service_name(args.name)
    svc = _registered_service(svc_name)
    stop_msg = services_mod.stop_service(svc)[1] if svc else "not registered"
    raw = _load_raw()
    removed = remove_service_entry(raw, svc_name)
    if removed:
        _save_raw(raw)
    return _emit(
        {"success": True, "service": svc_name, "removed": removed, "result": stop_msg},
        json_mode,
        [f"Service {svc_name}: {stop_msg}; "
         + ("entry removed from ~/.agentwire/config.yaml" if removed else "no entry to remove")
         + f". Buddy '{args.name}' is still registered."],
    )


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
    reg.add_argument("--voice", default="", metavar="VOICE", help=_VOICE_HELP)
    reg.set_defaults(func=beta_gated(cmd_buddy_register))

    _common(sub.add_parser("status", help="Show the buddy's identity and mail counts")).set_defaults(
        func=beta_gated(cmd_buddy_status)
    )

    lst = sub.add_parser("list", help="List registered buddies")
    lst.add_argument("--json", action="store_true", help="Output JSON")
    lst.set_defaults(func=beta_gated(cmd_buddy_list))

    unreg = _common(sub.add_parser("unregister", help="Remove the buddy's identity"))
    unreg.add_argument("--purge", action="store_true",
                       help="Also drop the spool, cursor and any pending mail")
    unreg.set_defaults(func=beta_gated(cmd_buddy_unregister))

    box = _common(sub.add_parser("inbox", help="Read mail other sessions sent the buddy"))
    box.add_argument("--all", action="store_true", help="Include already-read messages")
    box.add_argument("--ack", action="store_true", help="Mark the returned messages read")
    box.set_defaults(func=beta_gated(cmd_buddy_inbox))

    tl = sub.add_parser("tools", help="Show the tool surface handed to the model")
    tl.add_argument("--json", action="store_true", help="Output JSON")
    tl.set_defaults(func=beta_gated(cmd_buddy_tools))

    call = _common(sub.add_parser("call", help="Run one tool without a microphone"))
    call.add_argument("tool", help="Tool name (see `agentwire buddy tools`)")
    call.add_argument("--arg", action="append", help="Tool argument as key=value (repeatable)")
    call.set_defaults(func=beta_gated(cmd_buddy_call))

    mint = _common(sub.add_parser("mint", help="Mint an ephemeral Realtime session"))
    mint.add_argument("--model", default="")
    mint.add_argument("--voice", default="", metavar="VOICE", help=_VOICE_HELP)
    mint.set_defaults(func=beta_gated(cmd_buddy_mint))

    srv = _common(sub.add_parser("serve", help="Serve the browser client on localhost"))
    srv.add_argument("--port", type=int, default=8788,
                     help="Port on 127.0.0.1 (default: 8788 — never a portal port)")
    srv.add_argument("--model", default="")
    srv.add_argument("--voice", default="", metavar="VOICE", help=_VOICE_HELP)
    srv.set_defaults(func=beta_gated(cmd_buddy_serve))

    lg = sub.add_parser(
        "log",
        help="Read the bridge's per-run traces (stalls, tool calls, server events)",
        description=(
            "Every bridge run writes ~/.agentwire/logs/buddy/<run-id>.jsonl: the page's "
            "log lines, Realtime event types, response.create decisions, state "
            "snapshots, tool calls with timings, and stall records. Redacted."
        ),
    )
    lg.add_argument("--run", default=None, metavar="ID",
                    help="Run id or prefix (default: latest; --stalls alone scans every run)")
    lg.add_argument("--tail", action="store_true", help="Follow the run as it is written")
    lg.add_argument("--stalls", action="store_true",
                    help="Only stalls and crash-shaped events (window errors, closes)")
    lg.add_argument("--list", action="store_true", help="List traced runs")
    lg.add_argument("-n", "--lines", type=int, default=200,
                    help="Last N events (default 200; 0 = all)")
    lg.add_argument("--json", action="store_true", help="Output JSON")
    lg.set_defaults(func=beta_gated(cmd_buddy_log))

    svc = sub.add_parser(
        "service",
        help="Run the bridge under the service supervisor (human-only)",
        description=(
            "Write (or remove) the agentwire-buddy entry in services.custom so the "
            "bridge is supervised: watchdog health checks, restart on failure, and "
            "optionally autostart with the portal. Human-only — run from your own "
            "terminal."
        ),
    )
    svc_sub = svc.add_subparsers(dest="service_command", required=True)
    inst = _common(svc_sub.add_parser(
        "install", help="Register the buddy if needed, add its service entry, start it"))
    inst.add_argument("--port", type=int, default=DEFAULT_PORT,
                      help=f"Port on 127.0.0.1 (default: {DEFAULT_PORT})")
    inst.add_argument("--autostart", action="store_true",
                      help="Also boot it on portal launch / agentwire up (default: off)")
    inst.add_argument("--no-start", action="store_true",
                      help="Write the entry without starting it now")
    inst.set_defaults(func=beta_gated(cmd_buddy_service_install))
    _common(svc_sub.add_parser(
        "uninstall", help="Stop the service and remove its entry (keeps the identity)")
    ).set_defaults(func=beta_gated(cmd_buddy_service_uninstall))
