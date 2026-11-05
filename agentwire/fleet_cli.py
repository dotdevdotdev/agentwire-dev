"""``agentwire fleet`` — the report-back ledger and fleet view (#1277).

The CLI is the single source of truth: ``--json`` is :func:`fleet.build_fleet`
verbatim, and ``doctor`` and the portal both call it. See :mod:`agentwire.fleet`.
"""

import json
import time

from . import fleet


def _age(seconds) -> str:
    if seconds is None:
        return "-"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def render_fleet_text(data: dict) -> str:
    """Human rendering of a :func:`fleet.build_fleet` result."""
    lines: list[str] = []
    for p in data["parents"]:
        live = "live" if p["live"] else "NOT LIVE"
        lines.append(f"{p['parent']} ({live})" + (f" — ledger {p['ledger']}" if p["ledger"] != "ok" else ""))
        for c in p["children"]:
            bits = [c["state"], c.get("topology") or "?"]
            if c.get("role"):
                bits.append(c["role"])
            if c.get("branch"):
                bits.append(f"branch={c['branch']}")
            if c.get("pr"):
                bits.append(f"PR {c['pr']['state']} {c['pr'].get('url') or ''}".strip())
            if c.get("last_report_at"):
                bits.append(f"reported {_age(int(time.time()) - int(c['last_report_at']))} ago")
            for label, key in (("inbox", "inbox_waiting_age_s"), ("dead", "dead_letter_age_s"),
                               ("report queued", "report_queued_age_s"),
                               ("report dead-lettered", "report_dead_letter_age_s")):
                if c.get(key) is not None:
                    bits.append(f"{label} {_age(c[key])}")
            flags = "".join(f"  [!! {f}]" for f in c["flags"])
            lines.append(f"  {c['session']}: {', '.join(bits)}{flags}")
        if not p["children"] and p["ledger"] == "ok":
            lines.append("  (no children)")
    if not data["parents"]:
        lines.append("No cohort ledgers — nothing has been spawned with a report-back.")
    if data["flagged"]:
        lines.append(f"\n{len(data['flagged'])} problem(s) flagged.")
    return "\n".join(lines)


def cmd_fleet(args) -> int:
    """Print the fleet. Read-only; exit 0 even when flags are raised."""
    from . import pane_manager

    parent = None
    if not getattr(args, "all", False):
        parent = getattr(args, "session", None) or pane_manager.get_current_session()
    data = fleet.build_fleet(parent, with_pr=not getattr(args, "no_pr", False))
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print(render_fleet_text(data))
    return 0


def register_fleet_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "fleet",
        help="Every session an orchestrator spawned: state, PR, report-back, stuck messages",
        description=(
            "Read-only view of a session's fan-out: per child its role/topology, "
            "state (running/idle/gone), branch and open PR, last report time, and "
            "the age of any message waiting in or dead-lettered from its inbox. "
            "Flags a child that went idle without reporting, a report queued for "
            "a session that is not live, a dead-lettered report, and a gone "
            "child still holding an open PR."
        ),
    )
    parser.add_argument("-s", "--session",
                        help="Orchestrator to show (default: the current tmux session)")
    parser.add_argument("--all", action="store_true",
                        help="Every orchestrator with a cohort ledger on this machine")
    parser.add_argument("--no-pr", action="store_true",
                        help="Skip the GitHub PR lookup (faster; offline)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.set_defaults(func=cmd_fleet)
