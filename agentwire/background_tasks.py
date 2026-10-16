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
  than truncates, so it is read across lines.

The pane is authoritative when it is readable, the transcript when it is not.
The transcript is history: a task started before an ``/exit`` + ``--resume``
looks pending forever, while the pane shows what the harness believes right
now. Disagreement is logged. When neither can be read the answer is *unknown*
and every caller behaves as it did before this module existed (fail open).

Policy: **defer, bounded**. While tasks are pending the idle is swallowed; the
harness re-wakes the session when the last task reports, that turn ends in a
fresh idle with zero pending, and the ordinary notice goes out. A stuck task
must not suppress forever, so after :data:`DEFER_BOUND` one *qualified* notice
is sent ("N background task(s) still running; not necessarily finished") and
deferral resumes until the count reaches zero.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

#: How long an idle may be swallowed before one qualified notice goes out.
DEFER_BOUND = 30 * 60

#: Pane lines inspected: the turn-end line and footer sit just above the prompt
#: box, but a 30-column pane wraps them over several rows.
PANE_TAIL_LINES = 24

_KIND = r"(?:shell|monitor|agent|task|workflow|remote\s+session)s?"
_STILL_RUNNING = re.compile(
    rf"((?:\d+\s+{_KIND}(?:\s*,\s*(?:and\s+)?)?\s*)+)\s+still\s+running", re.IGNORECASE)
_FOOTER = re.compile(
    rf"⏵⏵[^\n]*?·\s*((?:\d+\s+{_KIND}(?:\s*,\s*)?\s*)+)", re.IGNORECASE)
_TURN_END = re.compile(r"✻\s+\S+\s+for\s+[^·\n]+·\s+done\s+\d{1,2}:\d{2}")
_COUNT = re.compile(rf"(\d+)\s+{_KIND}", re.IGNORECASE)
#: Glyphs of the input box / mode line; absent = this is not a readable agent pane.
_PANE_MARKERS = ("❯", "⏵⏵")

_TASK_STATUS = re.compile(r"<status>[^<]+</status>")
_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")


@dataclass
class Pending:
    """What is known about a session's background work.

    ``count`` is ``None`` when unknown (nothing readable) — callers must treat
    that as "behave as before", never as zero-and-proceed with confidence.
    """

    count: "int | None"
    source: str  # pane | transcript | unknown
    transcript_ids: "list[str] | None" = None
    pane_count: "int | None" = None
    notes: list[str] = field(default_factory=list)


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


def _sum_counts(fragment: str) -> int:
    return sum(int(n) for n in _COUNT.findall(fragment))


def pane_background_count(text: str) -> "int | None":
    """Background tasks the pane says are running; ``None`` if not an agent pane.

    The turn-end line (``✻ Worked for 3s · done 7:37 PM · 1 shell still running``)
    is located first and only ITS text, up to the input box rule, is searched:
    older turn lines stay on screen, and a stale ``still running`` above a
    current line without one must not outvote it. ``\\s`` spans newlines, so a
    30-column pane that wraps the line still matches; the footer there is
    truncated (``on ·``) and is only a corroborating signal.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()][-PANE_TAIL_LINES:]
    tail = "\n".join(lines)
    if not any(marker in tail for marker in _PANE_MARKERS):
        return None
    found = 0
    turn_ends = list(_TURN_END.finditer(tail))
    if turn_ends:
        # `─` opens the input-box rule, the end of the turn-end line's wrap.
        segment = tail[turn_ends[-1].start():].split("─", 1)[0]
        matches = _STILL_RUNNING.findall(segment)
        if matches:
            found = _sum_counts(matches[-1])
    footer = _FOOTER.findall(tail)
    if footer:
        found = max(found, _sum_counts(footer[-1]))
    return found


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
    pane_count = pane_background_count(captured) if captured is not None else None
    if pane_count is None:
        notes.append("pane_unreadable")

    if pane_count is not None:
        if ids and pane_count == 0:
            notes.append("stale_transcript")
        elif ids is not None and not ids and pane_count > 0:
            notes.append("pane_only")
        return Pending(pane_count, "pane", ids, pane_count, notes)
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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def clear_marker(session: str, pane: int) -> None:
    try:
        _marker(session, pane).unlink()
    except OSError:
        pass


def qualifier(count: int, waited: float) -> str:
    plural = "" if count == 1 else "s"
    return (f"is idle, but {count} background task{plural} still running "
            f"(waited {int(waited // 60)}m; not necessarily finished)")


def gate(
    session: str, pane: int = 0, conversation_id: str = "", cwd: str = "",
    now: "float | None" = None, pending: "Pending | None" = None,
) -> dict:
    """Decide what an idle event should do. Pure of side effects but the marker.

    ``action``: ``defer`` (swallow — the harness re-wakes the session),
    ``proceed`` (behave as before, including when nothing could be read), or
    ``qualify`` (proceed, but the notice must say tasks are still running).
    """
    now = time.time() if now is None else now
    pending = pending or evaluate(session, pane, conversation_id, cwd)
    out = {"action": "proceed", "pending": pending.count, "source": pending.source,
           "notes": pending.notes, "text": ""}

    if pending.count is None:
        log_event("unknown", session=session, pane=pane, notes=pending.notes)
        return out
    if not pending.count:
        if read_marker(session, pane):
            clear_marker(session, pane)
            log_event("cleared", session=session, pane=pane)
        if "stale_transcript" in pending.notes:
            log_event("stale_transcript", session=session, pane=pane,
                      ids=pending.transcript_ids)
        return out

    marker = read_marker(session, pane) or {"first_deferred_at": now, "qualified": False}
    marker.update(count=pending.count, conversation_id=conversation_id, cwd=cwd,
                  last_seen_at=now)
    waited = now - float(marker.get("first_deferred_at", now))
    if waited >= DEFER_BOUND and not marker.get("qualified"):
        marker["qualified"] = True
        out.update(action="qualify", text=qualifier(pending.count, waited))
        log_event("qualified", session=session, pane=pane, count=pending.count,
                  waited=int(waited))
    else:
        out["action"] = "defer"
        log_event("deferred", session=session, pane=pane, count=pending.count,
                  source=pending.source)
    _write_marker(session, pane, marker)
    return out


def tick(now: "float | None" = None) -> dict:
    """Watchdog stage: the bound must fire even if no further idle event does.

    A stuck background shell never wakes its session, so the hook alone would
    suppress forever. This sweep re-evaluates each deferred session; past the
    bound it sends the one qualified notice (pane-0 sessions with a parent),
    and drops markers whose session is gone or whose tasks finished.
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
        if not pending.count:
            if pending.count == 0:
                clear_marker(session, pane)
                result["cleared"].append(session)
            continue
        waited = now - float(marker.get("first_deferred_at", now))
        if waited < DEFER_BOUND or marker.get("qualified"):
            continue
        marker["qualified"] = True
        _write_marker(session, pane, marker)
        resolved = prompt_router.resolve_parent(session, pane)
        if pane != 0 or not resolved or services.is_service_session(session):
            continue
        text = qualifier(pending.count, waited)
        try:
            inbox.enqueue(to=resolved[0], text=text, kind="idle", sender=session)
        except (ValueError, OSError) as exc:
            log_event("qualify_failed", session=session, error=str(exc))
            continue
        log_event("qualified", session=session, pane=pane, count=pending.count,
                  waited=int(waited), via="tick")
        result["qualified"].append(session)
    return result
