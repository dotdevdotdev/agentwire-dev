"""Is a background task still pending when a session goes idle? (#1103)

The idle hook fires on "turn ended". A session that started a background
shell or monitor and ended its turn ("waiting on the suite, then I'll send the
verdict") is idle but NOT done: the harness wakes it again when the task
reports. Everything that reads idle as done (the parent's idle notice, the
worker-pane reap, the scheduled-task summary/``/exit`` pass) was wrong for it.

Signals, measured on a real Claude Code 2.1.302 session with a pending
``sleep 100`` and a pending Monitor:

* **Transcript.** A background start is a ``user`` row whose ``toolUseResult``
  carries ``backgroundTaskId`` (Bash) or ``taskId`` (Monitor). The end is a
  ``queue-operation`` enqueue whose content is a ``<task-notification>`` with a
  ``<status>`` element. A Monitor's per-event notifications have NO
  ``<status>`` and are not terminal. Pending = started - terminated.
* **Pane.** The turn-end line renders ``✻ Worked for 3s · done 7:37 PM · 1 shell
  still running`` and the footer ``⏵⏵ bypass permissions on · 1 shell``. At 30
  columns the footer is truncated (``on ·``) but the turn-end line WRAPS rather
  than truncates, so it is read across lines. Both are read only at their LIVE
position (directly above / below the last input box), never from scrollback.

The pane is authoritative when it is readable, the transcript when it is not.
The transcript is history: a task started before an ``/exit`` + ``--resume``
looks pending forever, while the pane shows what the harness believes right
now. Disagreement is logged.

Policy: **defer, bounded, and the bound releases a notice, never a kill.**
While tasks are pending the idle is swallowed; the harness re-wakes the session
when the last task reports, that turn ends in a fresh idle with zero pending,
and the ordinary notice goes out. A stuck task must not suppress forever, so
after :data:`DEFER_BOUND` one *qualified* parent notice is sent. The worker-pane
reap and the scheduled-task ``/exit`` are NEVER released by a timer: they stay
held for as long as a task may be running.

Uncertainty rule: *pending* wins every doubt. Unfamiliar task wording next to
"still running", a truncated footer with no turn-end line, an unparsable line
all read as **uncertain = hold** — a wrongly held session costs a late notice
(bounded, reported); a wrongly released one kills live work. Only "nothing
readable at all" (no agent pane, no transcript) behaves as before the gate.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

#: How long an idle may be swallowed before one qualified notice goes out.
DEFER_BOUND = 30 * 60

_RULE = re.compile(r"^[─━\-]{3,}$")
#: Glyphs that open a transcript row (assistant text, user echo, tool result);
#: a continuation row of the turn-end line never starts with one.
_ROW_GLYPHS = ("⏺", "❯", "⎿", "─")
_TURN_END = re.compile(
    r"✻\s.*?\bdone\s+\d{1,2}:\d{2}\s*(?:[AP]M)?\s*(?:·\s*)?(?P<rest>.*)$", re.IGNORECASE)
_STILL = re.compile(r"^(?P<frag>.*?)\s*\bstill\s+running\b", re.IGNORECASE)
_PIECE = re.compile(r"^(?P<n>\d+)\s+[A-Za-z][A-Za-z -]*$")
#: How many wrapped rows the turn-end line may occupy (20 columns wrap to 4).
_MAX_TURN_ROWS = 6

_TASK_STATUS = re.compile(r"<status>[^<]+</status>")
_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")


@dataclass
class Pending:
    """What is known about a session's background work.

    ``count`` is ``None`` when unknown. With ``uncertain`` False that means
    nothing was readable and callers behave as before; with ``uncertain`` True
    the pane was readable but the parse was not certain, which holds.
    """

    count: "int | None"
    source: str  # pane | transcript | unknown
    transcript_ids: "list[str] | None" = None
    pane_count: "int | None" = None
    notes: list[str] = field(default_factory=list)
    #: The pane shows the shape of background work but the parse is not
    #: certain (new task-kind wording, truncated footer, wrap). Treated as
    #: PENDING by every caller: a hold, never a reap (#1110 review).
    uncertain: bool = False


def _config_dir() -> Path:
    # Through the MODULE so a patched CONFIG_DIR is honored (see auth_expired).
    from . import core

    return Path(core.CONFIG_DIR)


def state_dir() -> Path:
    return _config_dir() / "idle-deferred"


def events_path() -> Path:
    return _config_dir() / "idle-deferred-events.jsonl"


def log_event(event: str, **fields) -> None:
    """Best-effort telemetry — never break the idle path."""
    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({"ts": time.time(), "event": event, **fields}) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Transcript signal
# ---------------------------------------------------------------------------


def pending_from_transcript(path: Path) -> list[str]:
    """Background task ids started and not yet terminated, in start order."""
    started: dict[str, None] = {}
    with open(path, errors="replace") as f:
        for line in f:
            # Cheap prefilter: most rows are neither a start nor a notification.
            if "backgroundTaskId" not in line and '"taskId"' not in line \
                    and "<task-notification>" not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rtype = row.get("type")
            if rtype == "queue-operation" and row.get("operation") == "enqueue":
                content = row.get("content")
                if isinstance(content, str) and "<task-notification>" in content \
                        and _TASK_STATUS.search(content):
                    m = _TASK_ID.search(content)
                    if m:
                        started.pop(m.group(1), None)
            elif rtype == "user":
                result = row.get("toolUseResult")
                if isinstance(result, dict):
                    tid = result.get("backgroundTaskId") or result.get("taskId")
                    if isinstance(tid, str) and tid:
                        started[tid] = None
    return list(started)


def find_transcript(session: str, conversation_id: str = "", cwd: str = "") -> "Path | None":
    """The live conversation's transcript: the id the hook was handed, else the
    newest id recorded for the session (#871)."""
    from .auth_expired import recorded_transcripts
    from .history import locate_conversation

    if conversation_id and cwd:
        found = locate_conversation(conversation_id, cwd).found_at
        if found:
            return Path(found)
    recorded = recorded_transcripts(session)
    return recorded[-1] if recorded else None


# ---------------------------------------------------------------------------
# Pane signal
# ---------------------------------------------------------------------------


@dataclass
class PaneReading:
    """``status``: ok (``count`` is certain) | uncertain | unreadable."""

    count: "int | None"
    status: str


def _sum_pieces(frag: str) -> "int | None":
    """Total of ``N <kind>`` pieces, or ``None`` if any piece is not that shape.

    The kind word is deliberately free text: new wording ("local agent",
    "bash", "hook") must count, not read as zero.
    """
    total = 0
    pieces = [p.strip() for p in re.split(r",|\band\b", frag) if p.strip()]
    if not pieces:
        return None
    for piece in pieces:
        m = _PIECE.match(piece)
        if not m:
            return None
        total += int(m.group("n"))
    return total


#: Nouns that make a footer ``N <noun>`` segment a background task. The footer
#: has no "still running" anchor, so a bare count next to an unknown noun
#: ("2 MCP servers failed", "3 files changed") is NOT a task. That is a
#: different case from a segment we cannot parse at all (cut after the digit),
#: which is uncertain: the two are tested separately.
TASK_NOUNS = frozenset({
    "shell", "monitor", "agent", "subagent", "task", "workflow", "bash", "hook",
    "session", "local", "remote", "background",
})


def _is_task_piece(piece: str) -> bool:
    words = re.findall(r"[a-z]+", piece.lower())
    return any(w.rstrip("s") in TASK_NOUNS for w in words)


def _footer_reading(rows: list[str]) -> PaneReading:
    """The live mode line (rows BELOW the input box only — a footer quoted in
    scrollback is above it and can never be mistaken for this one).

    ``count`` 0 and ``None`` are different values: a digit-bearing segment we
    cannot parse (``· 1``, ``· 1 shell, 1``) or a line cut off by the width is
    UNCERTAIN, never zero.
    """
    mode = next((r for r in rows if "⏵⏵" in r), None)
    if mode is None:
        return PaneReading(None, "uncertain")
    if mode.rstrip().endswith(("·", "…")):
        return PaneReading(None, "uncertain")  # truncated: a count may be cut off
    total = 0
    for segment in mode.split("·")[1:]:
        if not re.search(r"\d", segment):
            continue  # "← for agents" and friends carry no count
        pieces = [p.strip() for p in re.split(r",|\band\b", segment) if p.strip()]
        for piece in pieces:
            m = _PIECE.match(piece)
            if m is None:
                return PaneReading(None, "uncertain")
            if _is_task_piece(piece):
                total += int(m.group("n"))
    return PaneReading(total, "ok")


#: Text that looks like pending background work when no input box was found
#: (tall draft, plan/permission/resume dialog over the box): hold, don't proceed.
_PENDING_LOOKING = re.compile(
    r"still\s+running|✻[^\n]*\bdone\s+\d{1,2}:\d{2}|⏵⏵[^\n]*·\s*\d", re.IGNORECASE)


def parse_pane(text: str) -> PaneReading:
    """Background tasks the pane says are running.

    Anchored on the LIVE structure: the last input box (rule / ``❯`` / rule).
    The turn-end line is the ``✻ … done 7:37 PM · 1 shell still running`` block
    directly above it (it wraps over several rows on a narrow pane); anything
    else on screen — an older turn line, a quoted footer — is history and is
    ignored. A turn with no turn-end line (interrupted, non-turn wake) falls
    back to the live footer, which at narrow widths truncates; a truncated
    footer is *uncertain*, never zero.
    """
    rows = [r.rstrip() for r in text.splitlines() if r.strip()][-60:]
    box = next((i for i in range(len(rows) - 3, -1, -1)
                if _RULE.match(rows[i].strip()) and rows[i + 1].lstrip().startswith("❯")
                and _RULE.match(rows[i + 2].strip())), None)
    if box is None:
        # No box: a bare shell or an exited agent has no signal and stays
        # "unreadable"; any pending-looking text (a dialog or a tall draft over
        # a live task) must HOLD, never proceed to the reap.
        looks = _PENDING_LOOKING.search("\n".join(rows))
        return PaneReading(None, "uncertain" if looks else "unreadable")
    footer = _footer_reading(rows[box + 3:])

    block = None
    for j in range(box - 1, max(box - 1 - _MAX_TURN_ROWS, -1), -1):
        raw = rows[j]
        if raw.startswith("✻"):  # column 0: tool output quoting one is indented
            block = " ".join(r.strip() for r in rows[j:box])
            break
        if not raw[:1].isspace() or raw.lstrip().startswith(_ROW_GLYPHS):
            break  # a continuation row is indented and opens with no glyph
    m = _TURN_END.search(block) if block else None
    if m is None:
        return footer

    still = _STILL.match(m.group("rest"))
    if not still:
        # A complete turn-end line without "still running": nothing pending
        # on the turn line; the footer can only add to that.
        # (A footer we could not parse does not undo a complete turn line.)
        return PaneReading(footer.count if footer.status == "ok" else 0, "ok")
    turn = _sum_pieces(still.group("frag"))
    if turn is None:
        return PaneReading(None, "uncertain")
    return PaneReading(max(turn, footer.count if footer.status == "ok" else 0), "ok")


def pane_background_count(text: str) -> "int | None":
    """Certain count, or ``None`` (unreadable or uncertain)."""
    reading = parse_pane(text)
    return reading.count if reading.status == "ok" else None


def _capture(session: str, pane: int) -> "str | None":
    from . import pane_manager

    try:
        return pane_manager.capture_pane(session, pane, lines=60)
    except Exception:  # noqa: BLE001 — an unreadable pane is "unknown", not an error
        return None


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


def evaluate(session: str, pane: int = 0, conversation_id: str = "", cwd: str = "") -> Pending:
    """How many background tasks is *session*.*pane* still running?"""
    notes: list[str] = []
    ids: "list[str] | None" = None
    try:
        transcript = find_transcript(session, conversation_id, cwd)
        if transcript is not None:
            ids = pending_from_transcript(transcript)
        else:
            notes.append("no_transcript")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"transcript_error:{exc}")

    captured = _capture(session, pane)
    reading = parse_pane(captured) if captured is not None else PaneReading(None, "unreadable")

    if reading.status == "ok":
        if ids and reading.count == 0:
            notes.append("stale_transcript")
        elif ids is not None and not ids and reading.count:
            notes.append("pane_only")
        return Pending(reading.count, "pane", ids, reading.count, notes)
    if reading.status == "uncertain":
        # The pane shows the shape of background work we could not count.
        # A transcript with live ids is still evidence; otherwise HOLD.
        notes.append("pane_uncertain")
        if ids:
            return Pending(len(ids), "transcript", ids, None, notes)
        return Pending(None, "pane", ids, None, notes, uncertain=True)
    notes.append("pane_unreadable")
    if ids is not None:
        return Pending(len(ids), "transcript", ids, None, notes)
    return Pending(None, "unknown", None, None, notes)


# ---------------------------------------------------------------------------
# Deferral state + gate
# ---------------------------------------------------------------------------


def _marker(session: str, pane: int) -> Path:
    return state_dir() / f"{session}.{pane}.json"


def read_marker(session: str, pane: int) -> "dict | None":
    try:
        data = json.loads(_marker(session, pane).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_marker(session: str, pane: int, data: dict) -> None:
    path = _marker(session, pane)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One tmp name per WRITER: hook-vs-hook and hook-vs-tick race on the same
    # marker, and a shared name made the loser's replace() raise.
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def clear_marker(session: str, pane: int) -> None:
    try:
        _marker(session, pane).unlink()
    except OSError:
        pass


def qualifier(count: "int | None", waited: float) -> str:
    what = ("background work may still be running" if not count else
            f"{count} background task{'' if count == 1 else 's'} still running")
    return f"is idle, but {what} (waited {int(waited // 60)}m; not necessarily finished)"


#: A marker untouched for longer than the bound belongs to a session that is
#: gone (a same-name session reborn must not inherit its clock). Deliberately
#: the bound itself, not shorter: a task that wakes the session every 12
#: minutes must still reach it with no watchdog loaded. Identity is the
#: conversation id when the hook supplies one, so the gap only matters for an
#: id-less marker.
MARKER_GRACE = DEFER_BOUND


def reap_path(session: str, pane: int) -> bool:
    """Does an idle here lead to a KILL (worker-pane reap, scheduled-task exit)?

    The bound may release a *notice*; it must never release a kill — a live
    task is not ended by a timer.
    """
    return pane != 0 or (_config_dir() / "tasks" / f"{session}.json").exists()


def gate(
    session: str, pane: int = 0, conversation_id: str = "", cwd: str = "",
    now: "float | None" = None, pending: "Pending | None" = None,
) -> dict:
    """Decide what an idle event should do. Pure of side effects but the marker.

    ``action``: ``defer`` (swallow — the harness re-wakes the session),
    ``proceed`` (behave as before; ONLY when nothing is pending, or nothing at
    all could be read), or ``qualify`` (parent notice only, past the bound).
    A pane whose idle would reap or ``/exit`` is never released by the bound.
    """
    now = time.time() if now is None else now
    pending = pending or evaluate(session, pane, conversation_id, cwd)
    out = {"action": "proceed", "pending": pending.count, "source": pending.source,
           "uncertain": pending.uncertain, "notes": pending.notes, "text": ""}

    held = pending.uncertain or bool(pending.count)
    if pending.count is None and not pending.uncertain:
        log_event("unknown", session=session, pane=pane, notes=pending.notes)
        return out
    if not held:
        if read_marker(session, pane):
            clear_marker(session, pane)
            log_event("cleared", session=session, pane=pane)
        if "stale_transcript" in pending.notes:
            log_event("stale_transcript", session=session, pane=pane,
                      ids=pending.transcript_ids)
        return out

    marker = read_marker(session, pane)
    if marker is not None and (
        now - float(marker.get("last_seen_at", now)) > MARKER_GRACE
        or (conversation_id and marker.get("conversation_id")
            and marker["conversation_id"] != conversation_id)
    ):
        marker = None  # a different incarnation of this name: fresh clock
    marker = marker or {"first_deferred_at": now, "qualified": False}
    marker.update(count=pending.count, uncertain=pending.uncertain,
                  conversation_id=conversation_id, cwd=cwd, last_seen_at=now)
    waited = now - float(marker.get("first_deferred_at", now))
    if waited >= DEFER_BOUND and not marker.get("qualified") and not reap_path(session, pane):
        marker["qualified"] = True
        out.update(action="qualify", text=qualifier(pending.count, waited))
        log_event("qualified", session=session, pane=pane, count=pending.count,
                  waited=int(waited))
    else:
        out["action"] = "defer"
        log_event("deferred", session=session, pane=pane, count=pending.count,
                  source=pending.source, uncertain=pending.uncertain)
    _write_marker(session, pane, marker)
    return out


def tick(now: "float | None" = None) -> dict:
    """Watchdog stage: the bound must fire even if no further idle event does.

    A stuck background shell never wakes its session, so the hook alone would
    suppress forever. This sweep re-evaluates each deferred session, keeps the
    marker fresh, and past the bound sends the one qualified notice — for a
    worker pane to its owning pane 0, for a pane-0 session to its parent. The
    ``qualified`` flag is set only after the enqueue succeeded; a failed one
    retries next tick. The session itself stays held either way.
    """
    from . import inbox, prompt_router, services
    from .cohort import session_exists

    now = time.time() if now is None else now
    result: dict = {"qualified": [], "cleared": []}
    try:
        markers = sorted(state_dir().glob("*.json"))
    except OSError:
        return result
    for path in markers:
        session, _, pane_s = path.stem.rpartition(".")
        if not session or not pane_s.isdigit():
            continue
        pane = int(pane_s)
        marker = read_marker(session, pane)
        if marker is None:
            continue
        if not session_exists(session):
            clear_marker(session, pane)
            result["cleared"].append(session)
            continue
        pending = evaluate(session, pane, marker.get("conversation_id", ""),
                           marker.get("cwd", ""))
        if pending.count is None and not pending.uncertain:
            continue  # nothing readable this tick: keep the marker, decide next tick
        if not (pending.uncertain or pending.count):
            clear_marker(session, pane)
            result["cleared"].append(session)
            continue
        marker.update(count=pending.count, uncertain=pending.uncertain, last_seen_at=now)
        waited = now - float(marker.get("first_deferred_at", now))
        if waited < DEFER_BOUND or marker.get("qualified") or marker.get("unroutable"):
            _write_marker(session, pane, marker)
            continue
        resolved = prompt_router.resolve_parent(session, pane)
        if not resolved or services.is_service_session(session):
            marker["unroutable"] = True  # nobody to tell; stated, not "sent"
            _write_marker(session, pane, marker)
            log_event("unroutable", session=session, pane=pane)
            continue
        try:
            inbox.enqueue(to=resolved[0], text=qualifier(pending.count, waited),
                          kind="idle", sender=session)
        except (ValueError, OSError) as exc:
            log_event("qualify_failed", session=session, error=str(exc))
            _write_marker(session, pane, marker)
            continue
        marker["qualified"] = True
        _write_marker(session, pane, marker)
        log_event("qualified", session=session, pane=pane, count=pending.count,
                  waited=int(waited), via="tick")
        result["qualified"].append(session)
    return result
