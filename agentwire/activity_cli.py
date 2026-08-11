"""``agentwire activity`` — what the fleet has been doing (#1016).

The read side of :mod:`~agentwire.fleet_activity`. It exists as a CLI verb for
two reasons, and the second is the load-bearing one:

1. "CLI is the single source of truth" — a store readable only from inside the
   process tree that writes it is exactly the shape that rule exists to prevent.
2. The voice layer dispatches **only** through the CLI (see
   ``voice_layer/tools.py``), so without this verb the buddy could not read the
   ledger that was built for it.

Read-only, by construction: nothing here writes. Producers record from inside
the surfaces that generate the events (idle, say, toast, scheduled run), never
from a verb an agent could be talked into calling.
"""

from __future__ import annotations

import json
from datetime import timedelta

from . import fleet_activity

#: Widest window the verb will look back over. The ledger is capped at
#: ``MAX_ENTRIES`` anyway, so this bounds the ANSWER rather than the read: a
#: request for a week of awareness is a request for history, and
#: ``agentwire scheduler history`` / the event logs are where history lives.
MAX_HOURS = 72


def cmd_activity_list(args) -> int:
    json_mode = getattr(args, "json", False)
    hours = max(1, min(MAX_HOURS, int(getattr(args, "hours", 12) or 12)))
    entries = fleet_activity.recent(
        limit=max(1, int(getattr(args, "limit", 50) or 50)),
        event=getattr(args, "event", "") or "",
        session=getattr(args, "session", "") or "",
        window=timedelta(hours=hours),
    )
    if json_mode:
        print(json.dumps({
            "success": True,
            "count": len(entries),
            "window_hours": hours,
            "ledger": str(fleet_activity.ledger_path()),
            "activity": entries,
        }, indent=2))
        return 0
    if not entries:
        print(f"No fleet activity recorded in the last {hours}h.")
        return 0
    for entry in entries:
        # The announced flag is shown, not hidden: "recorded" and "said out
        # loud" are different facts, and an operator debugging a chatty or a
        # silent buddy needs to see which one an entry was.
        mark = "*" if entry.get("announced") else " "
        print(f"{mark} {entry.get('ts', '')}  {entry.get('event', ''):<16} "
              f"{entry.get('text', '')}")
    print()
    print("* = also queued to fleet-alert subscribers (spoken by the voice buddy at a gap)")
    return 0


def register_activity_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "activity",
        help="What the fleet has been doing (idle, tasks, toasts, speech)",
        description=(
            "The fleet's activity ledger: sessions going idle, scheduled tasks "
            "finishing, toasts shown to the owner, and everything spoken aloud "
            "through fleet TTS. This is AWARENESS — a record something can read "
            "when asked. Only a short, ruled subset is also queued to "
            "fleet-alert subscribers (see `agentwire alerts`); the rest waits "
            "here to be looked up."
        ),
    )
    sub = parser.add_subparsers(dest="activity_command", required=True)

    p = sub.add_parser("list", help="Recent activity, newest first")
    p.add_argument("--limit", type=int, default=50, help="Max entries (default: 50)")
    p.add_argument("--hours", type=int, default=12,
                   help=f"How far back to look (1-{MAX_HOURS}, default: 12)")
    p.add_argument("--event", choices=fleet_activity.EVENTS, help="Only this event type")
    p.add_argument("-s", "--session", help="Only this session (exact name)")
    p.add_argument("--json", action="store_true", help="Output JSON")
    p.set_defaults(func=cmd_activity_list)
