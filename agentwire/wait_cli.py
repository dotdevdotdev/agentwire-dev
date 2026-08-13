"""``agentwire wait`` — block on a fan-out's children (#852).

The join primitive a parent calls after fanning out. Blocking inside a tool
call is not idle, so the idle hook never fires: the parent is never prompted
for a roll-up it can't write yet, and is never ``/exit``-ed while its children
are still working. See :mod:`agentwire.cohort`.
"""

import json

from . import cohort


def cmd_wait(args) -> int:
    """Block until the calling session's cohort resolves (or time runs out).

    Bounded (``--timeout``) and re-callable, so a fan-out longer than the
    calling harness's tool timeout just loops. Each pass collects any report
    waiting in the inbox, kills the child it collected (collect-then-kill: the
    reverse order dead-letters the report and emails the owner), and marks a
    child whose session already vanished. Children still outstanding when the
    cohort deadline passes are killed and returned as failures so the parent's
    summary names them instead of the run leaking a live session.

    Exit status is 0 when the cohort resolved, 1 when this call returned with
    children still pending — a caller looping on it can just re-run.
    """
    from . import pane_manager

    session = getattr(args, "session", None) or pane_manager.get_current_session()
    json_mode = getattr(args, "json", False)
    if not session:
        msg = "Not in a tmux session — pass -s <session>"
        if json_mode:
            print(json.dumps({"success": False, "error": msg}))
        else:
            print(msg)
        return 1

    result = cohort.wait(session, timeout=getattr(args, "timeout", 600.0))

    if json_mode:
        print(json.dumps({"success": True, **result}, indent=2))
        return 0 if result["resolved"] else 1

    if not result.get("cohort"):
        print(f"No cohort registered for '{session}' — nothing to wait on.")
        return 0
    for entry in result["reports"]:
        print(f"── {entry['session']} ──")
        print(entry["report"] or "(no report text)")
    for entry in result.get("idle") or []:
        print(f"── {entry['session']} ── IDLE WITHOUT REPORT: the child went "
              "idle without sending a report — its work may not have happened")
    for entry in result["failed"]:
        state = "never reported" if entry["state"] == "timeout" else "session gone"
        print(f"── {entry['session']} ── FAILED: {state}")
    if result["left_alive"]:
        print("left running (worktree — teardown follows merge verification): "
              + ", ".join(result["left_alive"]))
    if result["pending"]:
        print(f"still pending: {', '.join(result['pending'])} "
              "— call `agentwire wait --children` again to keep waiting")
        return 1
    idle = result.get("idle") or []
    print(f"cohort resolved: {len(result['reports'])} reported, "
          f"{len(idle)} idle-without-report, {len(result['failed'])} failed")
    if idle:
        # Loud on purpose (#952): a parent that reads "0 failed" will proceed,
        # and here proceeding once meant treating unreviewed code as reviewed.
        print("WARNING: "
              + ", ".join(e["session"] for e in idle)
              + " resolved WITHOUT reporting — verify their work actually "
                "happened before acting on this cohort.")
    return 0


def register_wait_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "wait",
        help="Block on this session's fan-out children until they report",
        description=(
            "Wait for the child sessions this session spawned (its cohort) to "
            "report back, collecting each report and tearing the child down. "
            "Blocking here is a tool call, not idle, so the idle handler will "
            "not reap the parent mid-fan-out."
        ),
    )
    parser.add_argument("--children", action="store_true",
                        help="Wait on the cohort (the only mode; implied)")
    parser.add_argument("-s", "--session",
                        help="Parent session (default: the current tmux session)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="Seconds to block in THIS call (default: 600). The "
                             "cohort's own deadline still bounds the fan-out.")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.set_defaults(func=cmd_wait)
