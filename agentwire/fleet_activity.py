"""What the fleet DID — the awareness tier, and the ruling on what earns a voice (#1016).

The buddy could already be *told* things: ``msg send --to buddy`` reaches its
spool, and :mod:`~agentwire.fleet_alerts` gives the machine's own detectors a
way to address it. What nothing did was emit the fleet's ordinary signals — a
session going idle, a scheduled task finishing, a toast shown to the owner,
anything spoken aloud through fleet TTS. So the buddy was blind to the two
things the owner most wants from it: it could not check in, and it could not
tell that the fleet had *already said* something out loud.

**The routing was never the hard part; the judgment is.** Everything in the
buddy's spool eventually gets SPOKEN — the notifier volunteers unread mail at a
gap (#962) and an ``escalation`` cuts across the buddy's own voice (#967).
There is no "quiet" kind. So a producer that pushes every lifecycle event into
the spool does not make the buddy aware, it makes it a narrator: a session that
goes idle after every conversational turn would buy an utterance every time.

Hence **two tiers, and the split is the whole design**:

**1. The LEDGER (this module's file) — awareness, never volunteered.** Every
event is appended here, and the buddy reads it only when asked ("what's been
happening?"). Nothing in it is pushed, so recording an event is free of the
interrupt question entirely, and the closed list of unprompted paths in
``instructions.VOICE_MODE`` stays closed — this adds a *pull*, not a fourth
push. This is also what makes the two audio surfaces one: every ``agentwire
say`` is recorded here, so the buddy knows what the owner has already heard
and can decline to repeat it as news.

**2. The SPOOL (ordinary typed mail, via fleet_alerts) — for what a person
would actually want interrupting a gap.** :data:`ANNOUNCE` is that ruling,
pinned as data. It is deliberately short, and each entry has to answer "would a
colleague walk over and tell me this?":

- ``session_idle`` — only for a session somebody DELEGATED (a recorded parent,
  or a worker/reviewer role, or a worktree checkout). That is the event the
  owner described wanting: work they handed off has finished and wants a
  decision. A root orchestrator going idle is a conversational turn ending,
  not news, and announcing it would fire after every exchange the owner has
  with their own session — the failure that retires a channel.
- ``task_completed`` — a scheduled task finished. The owner did not watch it
  start and cannot see it end; this is the canonical "check in and offer a
  summary" event. A failed one is a ``request`` rather than a ``done``, by the
  same inherit-rule shape ``fleet_alerts`` already uses for dead letters: the
  fleet has made a judgment and the kind should carry it.
- ``toast_high`` — a ``notify-user --priority high`` toast. The one notify
  surface that already DECLARES urgency, and it declares it about a screen the
  owner may not be looking at. An ordinary toast does not qualify; it is
  ledger-only.

**Not announced, and on purpose.** ``spoke`` (an ``agentwire say``) is never
announced, whatever it said: the owner already heard it, and a channel that
repeats audio back at you is worse than one that stays quiet. Portal lifecycle
churn (``session_created``/``session_closed``/``pane_died``) is ledger-only —
true, cheap, and not something anyone wants read aloud. Neither of those is an
oversight; both are answers.

**Nothing here interrupts.** No lifecycle event is ever an ``escalation``. The
interrupt tier stays exactly the two conditions ``fleet_alerts`` already ruled
on — the ones nothing clears without a human. An idle session is not burning.

**Throttled per subject, from the ledger itself.** A flapping session must not
be able to buy an utterance per flap, so an announcement is suppressed when an
announced record for the same (event, subject) sits inside
:data:`ANNOUNCE`'s cooldown. The check reads the ledger rather than a second
state file — the record is written BEFORE the emit decision, so there is
exactly one thing to keep consistent.

**Every entry point is best-effort.** A producer's job is to do its own job:
``agentwire say`` must speak and the scheduler must finish its dispatch even if
this file is unwritable. Failures are logged into the fleet-alert event log
rather than swallowed, because a producer that cannot be distinguished from a
quiet fleet is the #885 shape.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from . import fleet_alerts

#: Sender stamped on announced activity. Distinct from ``fleet_alerts.SENDER``
#: so a recipient (and the dead-letter detector) can tell the machine's own
#: alarms apart from the fleet's ordinary comings and goings. Defined THERE and
#: re-exported here: the recursion guard in ``inbox`` has to name it, and a
#: second literal is how the two drift.
SENDER = fleet_alerts.ACTIVITY_SENDER

#: Events the ledger accepts. A closed set: an unknown event name is a coding
#: bug at a call site, and a ledger that accepts anything is one nothing can
#: query by kind.
EVENTS = (
    "session_idle",       # a pane-0 session finished a turn and went quiet
    "task_completed",     # a scheduled task run ended (any status)
    "toast_high",         # notify-user --priority high
    "toast",              # notify-user --priority normal
    "spoke",              # agentwire say — the owner has ALREADY heard this
    "session_created",
    "session_closed",
    "pane_died",
)

#: THE RULING — which events reach the buddy's spool, and how long before the
#: same subject may do it again. Membership means "worth interrupting a gap
#: for"; absence means ledger-only. The kind itself lives in
#: ``fleet_alerts.DETECTOR_KINDS`` so that "what may interrupt" stays answerable
#: in one place across detectors and lifecycle alike.
#:
#: Cooldowns are per (event, subject), and each one is a claim about how often
#: the *underlying thing* can honestly be news:
#:
#: - ``session_idle`` 15m — a worker can go idle, be nudged, and go idle again
#:   within a minute; the owner wants "it finished", not each oscillation.
#: - ``task_completed`` 2m — scheduled runs are minutes-to-hours apart, so this
#:   only collapses a retry storm on one task name. Two genuinely different
#:   tasks finishing together are two different subjects and both are said.
#: - ``toast_high`` 5m — a session that loops on a high-priority toast is the
#:   realistic bad case, and it must not buy an utterance per loop.
ANNOUNCE: dict[str, timedelta] = {
    "session_idle": timedelta(minutes=15),
    "task_completed": timedelta(minutes=2),
    "toast_high": timedelta(minutes=5),
}

#: How many entries the ledger keeps, and the slack before a trim runs. The
#: file is read whole by :func:`recent` and by the throttle check, so it is
#: bounded rather than rotated: the buddy wants "recently", never "since
#: forever", and an unbounded append would put a growing read on every
#: producer's path.
#: The trim is AMORTIZED, not exact: it fires only above ``TRIM_AT``, so the
#: file lives between ``MAX_ENTRIES`` and ``TRIM_AT`` rather than at a fixed
#: length. Rewriting on every append would put a whole-file write on the say
#: path for nothing.
MAX_ENTRIES = 1000
TRIM_AT = 1300

#: Floor on a serialized entry's length, for the cheap size check in
#: :func:`_trim`. Deliberately far below a real line (the ISO timestamp alone is
#: 32 bytes and every entry carries five more keys) — this must never skip a
#: trim that was due, and being conservative only costs an occasional read.
_MIN_LINE = 60

#: Default window for :func:`recent`. Anything older is history, not awareness.
DEFAULT_WINDOW = timedelta(hours=12)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _config_dir() -> Path:
    """Read through the MODULE, never a from-import (#902) — see fleet_alerts."""
    from . import core

    return Path(core.CONFIG_DIR)


def ledger_path() -> Path:
    return _config_dir() / "fleet-activity.jsonl"


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _read_entries() -> list[dict]:
    """Every ledger entry in append order. Unparseable lines are skipped.

    A truncated tail (a producer killed mid-write) costs one entry, never the
    file: the same tolerance ``delivery._entries`` applies to the spool.
    """
    path = ledger_path()
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in raw:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _trim(path: Path) -> None:
    """Keep the ledger bounded. Best-effort, and never destructive on failure.

    Rewritten through a temp file + ``os.replace`` so a crash mid-trim leaves
    the old ledger intact rather than a half-written one — the same discipline
    ``fleet_alerts._write_index`` uses, for the same reason.
    """
    try:
        # A stat before a read: the trim runs on every append, and the common
        # case is a ledger nowhere near the cap. ``_MIN_LINE`` is a floor on a
        # line's length (the timestamp alone is longer), so a file under this
        # size cannot hold TRIM_AT entries and needs no read to prove it.
        if path.stat().st_size < TRIM_AT * _MIN_LINE:
            return
    except OSError:
        return
    entries = _read_entries()
    if len(entries) <= TRIM_AT:
        return
    keep = entries[-MAX_ENTRIES:]
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


def record(
    event: str,
    *,
    session: str = "",
    text: str = "",
    subject: str = "",
    announced: bool = False,
    **detail,
) -> dict:
    """Append one activity entry. Returns it; never raises.

    *subject* is what the throttle groups by — the session for a lifecycle
    event, the task name for a scheduled run. It defaults to *session* so a
    caller can only get it wrong by trying.
    """
    if event not in EVENTS:
        # A coding bug, not an environment failure — but a producer must not
        # die of it either. Recorded loudly enough to be found.
        fleet_alerts.log_event("activity_unknown_event", activity_event=event)
        return {}
    entry = {
        "ts": _now().isoformat(),
        "event": event,
        "session": session,
        "subject": subject or session,
        "text": str(text)[:2000],
        "announced": bool(announced),
        **{k: v for k, v in detail.items() if v not in (None, "")},
    }
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim(path)
    except (OSError, TypeError, ValueError) as exc:
        fleet_alerts.log_event("activity_write_failed", activity_event=event, error=str(exc))
    return entry


def recent(
    limit: int = 50,
    *,
    event: str = "",
    session: str = "",
    window: "timedelta | None" = None,
) -> list[dict]:
    """The most recent entries, newest first, within *window* (default 12h).

    Filters are exact matches, never prefixes: the buddy resolves a session
    name from ``fleet_sessions`` before asking, and a fuzzy match here would
    answer confidently about the wrong session.
    """
    cutoff = _now() - (window or DEFAULT_WINDOW)
    out: list[dict] = []
    for entry in reversed(_read_entries()):
        if event and entry.get("event") != event:
            continue
        if session and entry.get("session") != session:
            continue
        try:
            when = datetime.fromisoformat(str(entry.get("ts")))
        except (TypeError, ValueError):
            continue
        if when < cutoff:
            # Append order is chronological, so the first entry older than the
            # window ends the walk — every remaining one is older still.
            break
        out.append(entry)
        if len(out) >= max(1, limit):
            break
    return out


def _announced_recently(event: str, subject: str, cooldown: timedelta) -> bool:
    """Has an ANNOUNCED entry for this (event, subject) landed inside *cooldown*?

    Reads the ledger rather than a second state file. The record is written
    before the emit decision, so there is one thing to keep consistent — and a
    lost ledger costs a duplicate announcement, never a lost one.
    """
    cutoff = _now() - cooldown
    for entry in reversed(_read_entries()):
        try:
            when = datetime.fromisoformat(str(entry.get("ts")))
        except (TypeError, ValueError):
            continue
        if when < cutoff:
            return False
        if (
            entry.get("event") == event
            and entry.get("subject") == subject
            and entry.get("announced")
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Record + (maybe) announce
# ---------------------------------------------------------------------------


def note(
    event: str,
    *,
    session: str = "",
    text: str = "",
    subject: str = "",
    kind: str = "",
    exclude: Iterable[str] = (),
    **detail,
) -> dict:
    """Record *event*, and announce it to fleet-alert subscribers if it earns one.

    Returns ``{"recorded": entry, "announced": [names]}``. Never raises: this
    sits inside producers whose real job is speaking, dispatching or notifying.

    *exclude* names sessions that must not receive the announcement. The event's
    own session is always excluded — a session hearing about itself is noise —
    and callers add whoever already heard it by another route (an idle child's
    parent gets ``notify-parent``; mailing it again is the same news twice).
    """
    subject = subject or session
    cooldown = ANNOUNCE.get(event)
    throttled = bool(cooldown) and _announced_recently(event, subject, cooldown)
    will_announce = bool(cooldown) and not throttled

    entry = record(
        event,
        session=session,
        text=text,
        subject=subject,
        announced=will_announce,
        **detail,
    )
    if not will_announce:
        return {"recorded": entry, "announced": [], "throttled": throttled}

    skip = {session, *(exclude or ())} - {""}
    try:
        reached = fleet_alerts.emit_for(
            event,
            text,
            kind=kind or None,
            exclude=skip,
            sender=SENDER,
        )
    except Exception as exc:  # noqa: BLE001  # awareness must not break producers
        fleet_alerts.log_event("activity_emit_failed", activity_event=event, error=str(exc))
        reached = []
    return {"recorded": entry, "announced": reached, "throttled": False}


# ---------------------------------------------------------------------------
# Producer helpers — one per surface, each owning its own wording
# ---------------------------------------------------------------------------


def _is_delegated(session: str) -> bool:
    """Did somebody hand this session its work?

    The gate on announcing an idle. True for a session with a recorded parent
    (``created_by``), a worker/reviewer ROLE, or a worktree checkout — the three
    independent ways #716 says "this is somebody's delegated work". False for a
    root orchestrator, whose idle is a conversational turn ending rather than a
    job finishing, and for anything with no record at all: an unknown session is
    not evidence of delegation, and the failure direction here is silence.
    """
    from . import core

    try:
        meta = core.load_session_metadata(session)
    except Exception:  # noqa: BLE001
        return False
    if not meta:
        return False
    if str(meta.get("created_by") or "").strip():
        return True
    if str(meta.get("role") or "") in {"worker", "reviewer"}:
        return True
    return bool(str(meta.get("worktree_path") or "").strip())


def note_session_idle(session: str, text: str, *, parent: str = "") -> dict:
    """A pane-0 session went idle. Announced only for DELEGATED work.

    The ``--on-idle`` producer already drops infrastructure services before it
    gets here (they cycle idle constantly and are nobody's delegated work), so
    this adds exactly one further question: did somebody hand this session its
    task? See :func:`_is_delegated` for why a root orchestrator is ledger-only.
    """
    # The caller's phrasing is already a predicate ("is idle and done working",
    # from the idle hook), so the session name PREFIXES it rather than being
    # joined to it — "child is idle — is idle and done working" is what the
    # obvious concatenation produces, and it is what the owner would hear.
    body = f"{session} {text}".strip() if text else f"{session} is idle"
    if not _is_delegated(session):
        return {"recorded": record("session_idle", session=session, text=body),
                "announced": [], "throttled": False}
    return note("session_idle", session=session, text=body, exclude=(parent,) if parent else ())


#: Run statuses whose CONDITION already has a detector of its own
#: (``usage_limit_park``, ``auth_expired`` in ``fleet_alerts``). Recorded, never
#: announced: the outage is machine-wide and its detector says so once, where
#: this producer would say it again per task — the same news twice, from the one
#: subsystem whose whole job is not doing that.
_DETECTOR_OWNED_STATUSES = frozenset({"usage_limit", "auth_expired"})

#: The one status that means the run did what it was asked. Spelled out rather
#: than "not failed": ``incomplete`` and ``timeout`` are not successes, and a
#: negated check would have quietly called them one.
_TASK_OK = "complete"


def note_task_completed(task: str, session: str, status: str, duration: int,
                        summary: str) -> dict:
    """A scheduled task run ended.

    The kind carries the fleet's own verdict: a clean run is ``done`` (news),
    anything else is ``request`` (it wants a person). Same inherit shape
    ``fleet_alerts`` uses for a dead-lettered escalation — the judgment was
    already made upstream, and flattening it here would throw it away.
    """
    ok = status == _TASK_OK
    head = f"scheduled task '{task}' {'finished' if ok else status or 'ended'}"
    body = f"{head}: {summary}".strip() if summary else head
    if status in _DETECTOR_OWNED_STATUSES:
        return {
            "recorded": record("task_completed", session=session, subject=task,
                               text=body, task=task, status=status, duration=duration),
            "announced": [],
            "throttled": False,
        }
    return note(
        "task_completed",
        session=session,
        subject=task,
        text=body,
        kind="" if ok else "request",
        task=task,
        status=status,
        duration=duration,
    )


def note_toast(text: str, *, session: str = "", priority: str = "normal") -> dict:
    """The owner was shown a desktop toast.

    High priority is announced; normal is ledger-only. The distinction is the
    caller's own declaration of urgency about a screen the owner may not be
    looking at — this layer does not second-guess it in either direction.
    """
    high = priority == "high"
    body = f"toast for the owner: {text}" if not session else f"toast from {session}: {text}"
    return note("toast_high" if high else "toast", session=session, text=body,
                priority=priority)


def note_spoke(text: str, *, session: str = "", sink: str = "") -> dict:
    """Something was spoken aloud through fleet TTS. NEVER announced.

    This is the entry that makes the two audio surfaces one. The owner has
    already heard it, so repeating it back would be the worst thing this
    feature could do; what the record buys is the opposite — the buddy can see
    what was said and decline to offer it as news.
    """
    return note("spoke", session=session, text=text, sink=sink)


def note_lifecycle(event: str, session: str, **detail) -> dict:
    """A portal lifecycle event (session created/closed, pane died). Ledger-only."""
    return note(event, session=session, text=f"{session}: {event}", **detail)
