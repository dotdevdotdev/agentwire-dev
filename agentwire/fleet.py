"""Fleet view — one read-only picture of every session an orchestrator spawned (#1277).

Delegation failed silently in three different ways (no parent, wrong parent
name, dead-lettered reports) and each was found hours later by the owner. The
data to see all three already exists in four places; this module is the ONE
reader that joins them:

* the cohort ledger (:mod:`agentwire.cohort`) — who spawned whom, and what each
  child's report-back resolved to;
* the worktree registry — a child's branch;
* the inbox (:mod:`agentwire.inbox`) — reports waiting for, or dead-lettered
  away from, the parent, plus messages waiting for the child itself;
* session records and tmux liveness.

``agentwire fleet --json`` is :func:`build_fleet`'s output verbatim; ``doctor``
and the portal call the CLI and render it — neither re-derives anything.

**Reads only.** No ledger is rewritten, no message touched, nothing killed.

**Absence is reported, never raised.** A corrupt ledger is a parent row whose
``ledger`` field says so; a child with no session record is a row with
``session_record: false``. A reader that crashes on the broken thing it exists
to expose is useless exactly when it matters.

Flags (``flags`` on a child, aggregated in ``flagged``):

``idle_no_report``
    The child went idle and the only thing it left was the idle handler's
    placeholder — its work may not have happened.
``report_queued_to_dead_session``
    A report from the child sits in the inbox of a parent that is not live;
    nothing will ever drain it.
``report_dead_lettered``
    A load-bearing message from the child was dead-lettered on the way to the
    parent.
``gone_with_open_pr``
    The child's session is gone but its branch still has an OPEN PR — nobody
    is positioned to act on it. PR state comes from
    :func:`agentwire.session_cli.pr_for_branch`, the same probe the dangling-PR
    scan uses.
``no_session_record``
    A live child with no launch record — its role and parent are unknowable.
"""

from __future__ import annotations

import time

from . import cohort, inbox, worktree_registry

IDLE_NO_REPORT = "idle_no_report"
REPORT_QUEUED_TO_DEAD = "report_queued_to_dead_session"
REPORT_DEAD_LETTERED = "report_dead_lettered"
GONE_WITH_OPEN_PR = "gone_with_open_pr"
NO_SESSION_RECORD = "no_session_record"

STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_GONE = "gone"


def _oldest_age_s(messages: list, now_ms: int) -> int | None:
    stamps = [m.ts for m in messages if m.ts]
    return max(0, (now_ms - min(stamps)) // 1000) if stamps else None


def _safe(fn, *args) -> list:
    """An inbox read that degrades to "nothing" — an unreadable inbox must not
    take the whole fleet view down with it."""
    try:
        return fn(*args)
    except (OSError, ValueError):
        return []


def _branches_by_session() -> dict[str, dict]:
    """Registry entry per worktree-topology session (pane entries name their
    OWNER, not a child, so they are not a branch of any session here)."""
    try:
        rows = worktree_registry.all_entries()
    except Exception:
        return {}
    return {r["session"]: r for r in rows
            if r.get("session") and r.get("topology", "worktree") == "worktree"}


def _child_row(parent: str, parent_live: bool, entry: dict, *, registry: dict[str, dict],
               parent_pending: list, parent_dead: list, now_ms: int,
               with_pr: bool) -> dict:
    from .core import load_session_metadata
    from .session_cli import pr_for_branch

    name = str(entry.get("session") or "")
    ledger_state = entry.get("state")
    live = bool(name) and cohort.session_exists(name)
    if not live:
        state = STATE_GONE
    elif ledger_state == cohort.IDLE:
        state = STATE_IDLE
    else:
        state = STATE_RUNNING

    record = load_session_metadata(name) if name else {}
    reg = registry.get(name) or {}
    branch = reg.get("branch")
    pr = pr_for_branch(branch, reg.get("project")) if (with_pr and branch) else None

    queued = [m for m in parent_pending if m.sender == name and m.kind in cohort.REPORT_KINDS]
    dead = inbox.load_bearing([m for m in parent_dead if m.sender == name])
    resolved_at = entry.get("resolved_at")

    row = {
        "session": name,
        "parent": parent,
        "role": record.get("role"),
        "roles": record.get("roles") or [],
        "topology": entry.get("topology"),
        "state": state,
        "ledger_state": ledger_state,
        "session_record": bool(record),
        "branch": branch,
        "pr": {"state": str(pr.get("state", "")).upper(), "url": pr.get("url")} if pr else None,
        "last_report_at": resolved_at if ledger_state == cohort.REPORTED else None,
        "inbox_waiting_age_s": _oldest_age_s(_safe(inbox.list_messages, name), now_ms) if name else None,
        "dead_letter_age_s": _oldest_age_s(_safe(inbox.list_dead, name), now_ms) if name else None,
        "report_queued_age_s": _oldest_age_s(queued, now_ms),
        "report_dead_letter_age_s": _oldest_age_s(dead, now_ms),
        "flags": [],
    }
    flags = row["flags"]
    if ledger_state == cohort.IDLE:
        flags.append(IDLE_NO_REPORT)
    if queued and not parent_live:
        flags.append(REPORT_QUEUED_TO_DEAD)
    if dead:
        flags.append(REPORT_DEAD_LETTERED)
    if state == STATE_GONE and row["pr"] and row["pr"]["state"] == "OPEN":
        flags.append(GONE_WITH_OPEN_PR)
    if live and not record:
        flags.append(NO_SESSION_RECORD)
    return row


def _parent_row(parent: str, *, registry: dict[str, dict], now_ms: int, with_pr: bool) -> dict:
    data, problem = cohort.read(parent)
    row = {
        "parent": parent,
        "live": cohort.session_exists(parent),
        "ledger": problem or "ok",
        "task": (data or {}).get("task"),
        "children": [],
    }
    if data is None:
        return row
    parent_pending = _safe(inbox.list_messages, parent)
    parent_dead = _safe(inbox.list_dead, parent)
    for entry in data["children"]:
        if not isinstance(entry, dict):
            continue
        row["children"].append(_child_row(
            parent, row["live"], entry, registry=registry, parent_pending=parent_pending,
            parent_dead=parent_dead, now_ms=now_ms, with_pr=with_pr))
    return row


def build_fleet(parent: str | None = None, *, with_pr: bool = True) -> dict:
    """The fleet view: one row per parent, one row per child under it.

    *parent* ``None`` means every ledger on the machine; otherwise just that
    parent (a parent with no ledger yields a row saying ``missing``, so a typo
    is visible rather than an empty success). Zero ledgers is a valid, empty
    fleet.
    """
    now_ms = int(time.time() * 1000)
    registry = _branches_by_session()
    parents = [parent] if parent else cohort.ledger_parents()
    rows = [_parent_row(p, registry=registry, now_ms=now_ms, with_pr=with_pr) for p in parents]
    flagged = [
        {"session": c["session"], "parent": r["parent"], "flag": f}
        for r in rows for c in r["children"] for f in c["flags"]
    ]
    broken = [{"parent": r["parent"], "ledger": r["ledger"]}
              for r in rows if r["ledger"] not in ("ok", "missing")]
    return {
        "success": True,
        "parents": rows,
        "flagged": flagged,
        "broken_ledgers": broken,
    }
