"""Fan-out cohort ledger — a parent's outstanding children (#852).

A scheduled task that fans out child sessions had no way to wait for them.
The parent went idle while *waiting*, the idle handler read idle as done, and
the task was reaped — killing the roll-up the parent existed to write and
orphaning the children's report-backs into dead-letter (which, for
load-bearing kinds, also emails the owner about work that succeeded). The
children themselves were full ``agentwire new`` sessions, so nothing reaped
them either: a nightly fan-out leaked N sessions per run.

**idle != done for a session with outstanding children.** This module is the
one artifact that makes that knowable, with three consumers:

1. ``agentwire wait --children`` (:func:`wait`) — the join primitive. Blocking
   inside a tool call is not idle, so the hook never fires and the agent
   resumes naturally to write its roll-up.
2. The idle-handler guard — a file-existence + ``jq`` check in the same shape
   as the usage-limit park guard and the #276 prompt-router guard. It reads
   ``state == "pending"`` and ``deadline`` directly out of the JSON, so those
   two field names are load-bearing across the language boundary.
3. The watchdog sweeper (:func:`tick`) — the anti-leak backstop. It reaps a
   cohort whose parent died mid-wait, and reconciles a live parent's ledger so
   the guard self-clears as children exit.

**Cohort is not rooting.** ``created_by`` (#715) answers "who has authority /
where do prompts route", and deliberately returns nothing for a *cross-project*
spawn. The 2026-08-01 fan-out was three cross-project children and one
same-project one, so a ``created_by``-derived guard would have protected
exactly one child and reaped the parent out from under the other three. A
half-linked fan-out is worse than an unlinked one, so cohort membership is
keyed off the CALLER, independent of the rooting decision.

**Ordering is load-bearing.** ``agentwire kill`` runs ``inbox.gc_sender()``,
which dead-letters the killed session's still-pending load-bearing outbound
**and emails the owner**. Kill-before-collect turns every child's ``done``
report into owner email. So: collect the report, consume it, THEN kill.

Everything here fails OPEN: a missing, corrupt, or expired ledger must never be
able to wedge a task alive forever.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .utils.event_log import append_event

COHORT_ROOT = Path.home() / ".agentwire" / "cohorts"
EVENTS_FILE = Path.home() / ".agentwire" / "cohort-events.jsonl"

# How long a cohort may suppress its parent's idle handling. Past this the
# guard stops firing, `wait` stops blocking, and outstanding children are
# killed and reported as failures — a wedged child must not pin a task forever.
DEFAULT_TTL = 3600

# How long past its deadline a ledger survives when the parent is still alive.
# Covers a parent that fanned out and never called `wait --children`: the guard
# has already stopped protecting it, and the children get reaped rather than
# leaked. Generous, because a parent that IS about to call `wait` should find
# its ledger intact.
STALE_GRACE = 3600

# Child states. `pending` is read by the idle-handler guard's jq — renaming it
# silently disarms the guard.
PENDING = "pending"
REPORTED = "reported"  # report collected, child killed
IDLE = "resolved_idle"  # child went idle WITHOUT reporting (#952) — the idle
                        # handler's synthetic placeholder arrived, no report did
TIMEOUT = "timeout"    # deadline hit with no report; child killed anyway
GONE = "gone"          # child's session vanished before it reported

RESOLVED_STATES = (REPORTED, IDLE, TIMEOUT, GONE)

# Topologies, which decide whether resolving a child also tears its session
# down. A `main`-topology child (`agentwire new`) is the leak this issue is
# about: nothing reaps it, so the cohort does. A `worktree` child is NOT torn
# down — it holds a branch and possibly an open PR, whose teardown must follow
# merge verification (#756), and its session is where a reviewer sends fix-ups.
# It is still tracked, still reported, and still surfaced when it goes silent;
# an abandoned one is what `worktree --dangling` / `--list` already flag.
MAIN = "main"
WORKTREE = "worktree"

# Kinds a child's report-back can arrive as. `note` is included because a child
# that fails mid-task may downgrade its report; `ingest` is not, since passive
# messages are pull-only pointers the parent reads separately.
#
# `voice` is deliberately absent (#985): it is the OWNER speaking through the
# buddy, never a child reporting on its task, so harvesting one into a cohort
# roll-up would attribute the owner's words to a worker. The seam this creates
# is worth knowing about, because the two halves filter on different fields:
# `inbox._cohort_held` holds by SENDER, so if the buddy ever were a pending
# child, its `voice` message IS held here while _harvest below skips it. That is
# a deferral, not a loss — the message stays pending and delivers once the
# cohort resolves, the same shape `ingest` already has. Pinned in
# tests/unit/test_voice_kind.py::TestCohortInteraction.
REPORT_KINDS = ("done", "request", "escalation", "note")

# The idle handler's synthetic placeholder kind (#952). Harvested so the child
# still RESOLVES promptly, but never counted as a report: a placeholder means
# "the child went idle without reporting", and `wait` says exactly that
# (state IDLE) instead of flattening it into `reported`. The discriminator is
# the KIND, deliberately not the message text — a sentinel string is defeated
# by any child that happens to write the same words as its genuine report.
IDLE_KIND = "idle"

_POLL_INTERVAL = 2.0


# =============================================================================
# Ledger I/O
# =============================================================================


def _now() -> int:
    return int(time.time())


def log_event(event: str, **fields) -> None:
    append_event(EVENTS_FILE, {"ts": _now(), "event": event, **fields})


def ledger_path(parent: str) -> Path:
    """Ledger file for *parent*. Worktree names contain ``/`` and nest a dir."""
    if not parent or parent.startswith("/") or ".." in parent.split("/"):
        raise ValueError(f"invalid session name: {parent!r}")
    path = COHORT_ROOT / f"{parent}.json"
    if not path.resolve().is_relative_to(COHORT_ROOT.resolve()):
        raise ValueError(f"invalid session name: {parent!r}")
    return path


def load(parent: str) -> dict | None:
    """The parent's ledger, or None when absent/unreadable/malformed.

    Fails open on purpose: a corrupt ledger reads as "no cohort", so the guard
    stops suppressing and the parent resumes normal idle handling.
    """
    try:
        data = json.loads(ledger_path(parent).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("children"), list):
        return None
    return data


def _save(parent: str, data: dict) -> None:
    path = ledger_path(parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _locked(parent: str):
    """Serialize a read-modify-write on one parent's ledger.

    An orchestrator can fire several spawns in a single turn (parallel tool
    calls), and a lost update there means a child is never enrolled — i.e. a
    session that leaks. Same mkdir-based lock the inbox uses; best-effort, so a
    stuck lock degrades to the unsynchronized write rather than blocking a
    spawn.
    """
    lock = ledger_path(parent).with_suffix(".lock")
    held = False
    for _ in range(50):
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            os.mkdir(lock)
            held = True
            break
        except FileExistsError:
            time.sleep(0.1)
        except OSError:
            break
    try:
        yield
    finally:
        if held:
            try:
                os.rmdir(lock)
            except OSError:
                pass


def discard(parent: str) -> bool:
    """Delete the parent's ledger. True if one was there."""
    try:
        ledger_path(parent).unlink()
        return True
    except (OSError, ValueError):
        return False


def enroll(parent: str, child: str, task: str | None = None,
           ttl: int = DEFAULT_TTL, topology: str = MAIN) -> bool:
    """Record *child* as an outstanding member of *parent*'s cohort.

    Called automatically by ``agentwire new`` (and therefore ``worktree``)
    whenever there is a caller, so a fan-out author has no bookkeeping to
    forget. Re-enrolling a session already in the ledger is a no-op; a fresh
    fan-out after the previous cohort resolved extends the deadline so the
    second wave gets its own full TTL.

    *topology* decides whether resolving this child also tears its session
    down — see :data:`WORKTREE`.
    """
    if not parent or not child or parent == child:
        return False
    try:
        with _locked(parent):
            data = load(parent) or {
                "parent": parent, "task": task, "created_at": _now(), "children": [],
            }
            if any(c.get("session") == child for c in data["children"]):
                return False
            data["children"].append({
                "session": child, "state": PENDING, "report": None,
                "topology": topology, "enrolled_at": _now(),
            })
            data["deadline"] = _now() + ttl
            if task and not data.get("task"):
                data["task"] = task
            _save(parent, data)
    except (OSError, ValueError):
        return False
    log_event("enrolled", parent=parent, child=child, task=task)
    return True


def pending(parent: str) -> list[str]:
    """Sessions in *parent*'s cohort that haven't been resolved yet."""
    data = load(parent)
    if not data:
        return []
    return [c["session"] for c in data["children"]
            if c.get("state") == PENDING and c.get("session")]


def blocking(parent: str, now: int | None = None) -> bool:
    """Should *parent*'s idle handling be suppressed right now?

    The Python mirror of the idle-handler's jq guard: pending children AND the
    deadline not yet passed. Both halves matter — the deadline is what stops a
    wedged child from pinning a task alive forever.
    """
    data = load(parent)
    if not data:
        return False
    if not any(c.get("state") == PENDING for c in data["children"]):
        return False
    return (now if now is not None else _now()) < int(data.get("deadline") or 0)


def _mark(data: dict, child: str, state: str, report: str | None = None) -> None:
    for entry in data["children"]:
        if entry.get("session") == child:
            entry["state"] = state
            if report is not None:
                entry["report"] = report


# =============================================================================
# Session liveness / teardown
# =============================================================================


def session_exists(session: str) -> bool:
    try:
        return subprocess.run(
            ["tmux", "has-session", "-t", f"={session}"],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _kill(session: str) -> bool:
    """Tear a child session down through the CLI's own kill path.

    Never the child's own job (a wedged child never goes idle, so child-side
    teardown structurally can't cover the failure path), and never raw tmux —
    the shared path also drops session metadata and GCs the child's leftover
    outbound.
    """
    try:
        from .pane_cli import kill_local_session

        return bool(kill_local_session(session).get("success"))
    except Exception:
        return False


# =============================================================================
# Collection + join
# =============================================================================


def _harvest(parent: str) -> dict[str, list]:
    """Pending inbox messages to *parent*, grouped by sender.

    Read straight off disk rather than waiting for the drain to paste them:
    the parent is mid-tool-call, and a paste into its box is exactly what
    #851 makes unreliable for long reports.
    """
    try:
        from . import inbox

        out: dict[str, list] = {}
        for msg in inbox.list_messages(parent):
            if msg.kind in REPORT_KINDS or msg.kind == IDLE_KIND:
                out.setdefault(msg.sender, []).append(msg)
        return out
    except Exception:
        return {}


def _consume(messages: list) -> str:
    """Take these messages out of the inbox and return their text.

    Consuming BEFORE the kill is what keeps ``gc_sender`` from dead-lettering
    the report (and emailing the owner) for a child that reported perfectly.
    """
    texts = []
    for msg in messages:
        texts.append(msg.text)
        try:
            if msg.path:
                msg.path.unlink()
        except OSError:
            pass
    return "\n".join(texts)


def collect(parent: str) -> dict | None:
    """One resolution pass over *parent*'s cohort. Returns the ledger, or None.

    Per pending child: a report waiting in the parent's inbox is consumed and
    the child torn down (collect → consume → kill, in that order); a child
    whose session has already vanished is marked ``gone``. Past the deadline
    every remaining child is torn down and marked ``timeout`` so the parent's
    summary can name it as a failure instead of the run silently leaking a live
    session. Teardown skips worktree children — see :func:`_teardown`.
    """
    data = load(parent)
    if not data:
        return None
    outstanding = [c["session"] for c in data["children"]
                   if c.get("state") == PENDING and c.get("session")]
    if not outstanding:
        return data

    expired = _now() >= int(data.get("deadline") or 0)
    reports = _harvest(parent)
    for child in outstanding:
        msgs = reports.get(child)
        if msgs:
            # A slot holding ONLY the idle handler's synthetic placeholder is
            # not a report (#952): the child went idle without saying anything.
            # Any genuine report kind alongside it wins — the child both
            # reported and idled, which is the normal happy path.
            real = [m for m in msgs if m.kind in REPORT_KINDS]
            placeholders = [m for m in msgs if m.kind == IDLE_KIND]
            if real:
                text = _consume(real)
                _consume(placeholders)  # clear the files, drop the synthetic text
                _mark(data, child, REPORTED, text)
                log_event("collected", parent=parent, child=child)
            else:
                _mark(data, child, IDLE, _consume(placeholders))
                log_event("child_idle_no_report", parent=parent, child=child)
            _teardown(data, child)
        elif not session_exists(child):
            _mark(data, child, GONE)
            log_event("child_gone", parent=parent, child=child)
        elif expired:
            _mark(data, child, TIMEOUT)
            _teardown(data, child)
            log_event("child_timeout", parent=parent, child=child)
    _save(parent, data)
    return data


def _teardown(data: dict, child: str) -> bool:
    """Kill a resolved child's session — unless it's on worktree topology.

    The leak #852 is about is a ``main``-topology child: a full session nothing
    reaps. A worktree child is deliberately left running: its branch and any
    open PR are torn down only after merge verification (#756), a reviewer may
    still need to send it fix-ups, and an abandoned one is already surfaced by
    ``worktree --dangling``. Killing it would trade a visible leak for a
    silently destroyed working tree.
    """
    entry = next((c for c in data["children"] if c.get("session") == child), None)
    if entry is not None and entry.get("topology") == WORKTREE:
        entry["torn_down"] = False
        return False
    killed = _kill(child)
    if entry is not None:
        entry["torn_down"] = killed
    return killed


def summarize(data: dict) -> dict:
    """Caller-facing view of a ledger."""
    children = data.get("children") or []
    return {
        "parent": data.get("parent"),
        "task": data.get("task"),
        "deadline": data.get("deadline"),
        "pending": [c["session"] for c in children if c.get("state") == PENDING],
        "reports": [{"session": c["session"], "report": c.get("report")}
                    for c in children if c.get("state") == REPORTED],
        # Resolved without a report (#952) — the child idled and the only
        # thing in its slot was the idle handler's placeholder. Silence is a
        # legitimate outcome; being counted as a report is not.
        "idle": [{"session": c["session"], "report": c.get("report")}
                 for c in children if c.get("state") == IDLE],
        "failed": [{"session": c["session"], "state": c.get("state")}
                   for c in children if c.get("state") in (TIMEOUT, GONE)],
        # Resolved but deliberately still running: worktree children, whose
        # branch/PR teardown follows merge verification (#756).
        "left_alive": [c["session"] for c in children
                       if c.get("state") in (REPORTED, IDLE, TIMEOUT)
                       and c.get("torn_down") is False],
        "children": children,
    }


def wait(parent: str, timeout: float = 600.0,
         poll: float = _POLL_INTERVAL) -> dict:
    """Block until *parent*'s cohort resolves, this call's *timeout* elapses,
    or the cohort deadline passes.

    Blocking here happens inside a tool call, which is not idle — so the idle
    hook never fires, the parent is never prompted for a roll-up it can't
    write, and it resumes naturally with every child's report in hand.

    Bounded and re-callable: a fan-out longer than the calling harness's tool
    timeout just loops. The ledger is deleted once nothing is pending, so the
    idle guard stops suppressing the moment the cohort is done.
    """
    data = load(parent)
    if not data:
        return {"parent": parent, "cohort": False, "resolved": True,
                "pending": [], "reports": [], "idle": [], "failed": [],
                "left_alive": [], "children": []}

    deadline = time.time() + max(timeout, 0)
    while True:
        data = collect(parent) or data
        result = summarize(data)
        if not result["pending"]:
            discard(parent)
            log_event("resolved", parent=parent,
                      reported=len(result["reports"]), idle=len(result["idle"]),
                      failed=len(result["failed"]))
            return {**result, "cohort": True, "resolved": True}
        if time.time() >= deadline:
            return {**result, "cohort": True, "resolved": False}
        time.sleep(poll)


# =============================================================================
# Watchdog sweep
# =============================================================================


def sweep() -> dict:
    """Reconcile every ledger on disk. The anti-leak backstop.

    - **Parent gone** → kill whatever is still pending and delete the ledger.
      This is the crash path (usage limit, guard deadline, ``/exit``) that
      leaks children under every other mechanism, including a child-side
      self-kill: a wedged child never goes idle, so it never self-kills.
    - **Parent alive** → mark pending children whose session has vanished, so
      the idle guard self-clears as the cohort empties.
    - **Past deadline + grace** → the parent fanned out and never joined; kill
      the stragglers and drop the ledger rather than leaving it to suppress
      nothing forever.

    Teardown here is deliberately session-only: killing a tmux session never
    touches a worktree's branch or its open PR, whose removal must follow merge
    verification (#756) and stays with ``worktree --remove``.
    """
    reaped: list[dict] = []
    swept: list[str] = []
    if not COHORT_ROOT.exists():
        return {"reaped": reaped, "swept": swept}

    for path in sorted(p for p in COHORT_ROOT.rglob("*.json") if p.is_file()):
        try:
            parent = str(path.relative_to(COHORT_ROOT))[: -len(".json")]
        except ValueError:
            continue
        data = load(parent)
        if not data:
            continue
        outstanding = [c["session"] for c in data["children"]
                       if c.get("state") == PENDING and c.get("session")]
        parent_live = session_exists(parent)
        expired = _now() >= int(data.get("deadline") or 0) + STALE_GRACE

        if parent_live and not expired:
            changed = False
            for child in outstanding:
                if not session_exists(child):
                    _mark(data, child, GONE)
                    changed = True
            if changed:
                _save(parent, data)
                swept.append(parent)
            continue

        for child in outstanding:
            if session_exists(child) and _teardown(data, child):
                reaped.append({"parent": parent, "child": child})
                log_event("orphan_reaped", parent=parent, child=child,
                          parent_live=parent_live, expired=expired)
        discard(parent)
        swept.append(parent)
    return {"reaped": reaped, "swept": swept}


def tick() -> dict:
    """Watchdog stage entry point — same shape as :func:`sweep`."""
    return sweep()
