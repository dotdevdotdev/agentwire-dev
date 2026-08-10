"""``agentwire alerts`` — who hears the machine's own detectors (#982).

Fleet detectors (expired login, usage-limit park, dead-lettered report-backs, a
root pane blocked with nowhere to route) have always been able to email the
OWNER. ``fleet_alerts`` lets them address a SESSION as typed mail instead, and
this is the surface that turns that on: without a CLI verb the subscription API
would be reachable only from inside the tree that calls it, which is precisely
the shape "CLI is the single source of truth" exists to prevent.

Subscription is a renewable **lease**, so ``list`` reports the expiry rather
than a boolean: a subscriber that stopped renewing has silently stopped
hearing anything, and that is a state an operator has to be able to see.
"""

from __future__ import annotations

import json

from . import fleet_alerts


def _emit(payload: dict, json_mode: bool, lines: "list[str] | None" = None) -> int:
    if json_mode:
        print(json.dumps(payload, indent=2))
    else:
        for line in lines or []:
            print(line)
    return 0 if payload.get("success", True) else 1


def cmd_alerts_subscribe(args) -> int:
    json_mode = getattr(args, "json", False)
    try:
        lease = fleet_alerts.subscribe(args.session)
    except Exception as exc:
        return _emit(
            {"success": False, "error": str(exc)}, json_mode, [f"error: {exc}"]
        )
    return _emit(
        {"success": True, "session": args.session, "lease": lease},
        json_mode,
        [
            f"'{args.session}' now receives fleet alerts.",
            f"  lease expires: {lease['expires_at']}",
            "  Renew before then — an expired lease goes quiet, it does not warn.",
        ],
    )


def cmd_alerts_unsubscribe(args) -> int:
    json_mode = getattr(args, "json", False)
    dropped = fleet_alerts.unsubscribe(args.session)
    if not dropped:
        return _emit(
            {"success": False, "error": f"'{args.session}' was not subscribed"},
            json_mode,
            [f"'{args.session}' was not subscribed."],
        )
    return _emit(
        {"success": True, "session": args.session},
        json_mode,
        [f"'{args.session}' no longer receives fleet alerts."],
    )


def cmd_alerts_list(args) -> int:
    """Who is subscribed — and whether this answer can be trusted.

    ``list`` reads the same candidate index the emit path does, so a lost index
    makes both of them say "nobody". That would be the worst property this
    command could have: the one surface built to make a silent stop VISIBLE
    would agree with the silent stop. So the index's presence is reported
    alongside the answer, and its absence is not rendered as a confident zero.
    """
    json_mode = getattr(args, "json", False)
    names = fleet_alerts.subscribers()
    index_present = fleet_alerts.subscribers_index_path().exists()
    rows = [{"session": n, "lease": fleet_alerts.subscription(n)} for n in names]
    if names:
        lines = [
            "Sessions receiving fleet alerts:",
            *(f"  {r['session']}  (lease expires {(r['lease'] or {}).get('expires_at')})"
              for r in rows),
        ]
    elif index_present:
        lines = ["No session is subscribed to fleet alerts."]
    else:
        lines = [
            "No subscriber index — so this is NOT a confident 'nobody subscribed'.",
            "Either nobody ever subscribed, or the index was lost; from here those "
            "look identical, and while it is missing every alert reaches nobody.",
            "Settle it (cheap, and it rebuilds the index either way):",
            "  agentwire alerts reindex",
        ]
    return _emit(
        {
            "success": True,
            "subscribers": names,
            "leases": rows,
            "index_present": index_present,
        },
        json_mode,
        lines,
    )


def cmd_alerts_reindex(args) -> int:
    """Rebuild the candidate index from the session records.

    The alert path never walks the record store — that cost sits on the
    synchronous permission-hook path — so a lost index means fewer alerts until
    somebody rebuilds it. This is that somebody.
    """
    json_mode = getattr(args, "json", False)
    names = fleet_alerts.reindex()
    return _emit(
        {"success": True, "subscribers": names},
        json_mode,
        [f"Reindexed: {len(names)} live subscriber(s)." , *(f"  {n}" for n in names)],
    )


def register_alerts_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "alerts",
        help="Who hears the machine's own detectors (fleet alerts)",
        description=(
            "Fleet detectors — expired login, usage-limit park, dead-lettered "
            "report-backs, a root pane blocked with nowhere to route — email the "
            "owner. They can also address a SESSION as typed mail: an escalation "
            "for the two conditions only a human can clear, a note or a request "
            "for the rest. A session subscribes here, on a renewable lease."
        ),
    )
    sub = parser.add_subparsers(dest="alerts_command", required=True)

    p = sub.add_parser("subscribe", help="Lease fleet alerts for a session")
    p.add_argument("session", help="Session name (must have a session record)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.set_defaults(func=cmd_alerts_subscribe)

    p = sub.add_parser("unsubscribe", help="Drop a session's fleet-alert lease")
    p.add_argument("session")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.set_defaults(func=cmd_alerts_unsubscribe)

    p = sub.add_parser("list", help="Sessions with a live lease, and its expiry")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.set_defaults(func=cmd_alerts_list)

    p = sub.add_parser("reindex", help="Rebuild the subscriber index from records")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.set_defaults(func=cmd_alerts_reindex)
