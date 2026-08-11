"""Event logging, live state, portal notifications, and board display."""

import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_config
from ..core import portal_request
from ..utils.event_log import append_event
from .models import Board, Schedule, TaskState
from .schedule import _compute_next_eligible, _is_in_flight


def _log_event(event: str, **fields) -> None:
    """Append an event to the scheduler JSONL log.

    Also the seam where a finished run becomes fleet awareness (#1016). Here
    rather than at the two dispatch call sites for the reason the CLAUDE.md
    worktree-path and session-name rulings keep re-teaching: a per-call-site
    copy is what drifts, and a third dispatch path would ship with no awareness
    and nothing to say so. ``task_completed`` is logged exactly once per run by
    both the in-place and worktree dispatchers, and it carries the whole story
    (status, duration, the parsed summary), which is what makes it the right
    event rather than the convenient one.
    """
    from agentwire import scheduler as _sched

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    if event == "task_completed":
        # The owner did not watch this start and cannot see it end — this is the
        # canonical "check in and offer a summary" event. Best-effort: a
        # dispatch must finish recording its own run whatever this does.
        from agentwire import fleet_activity

        try:
            fleet_activity.note_task_completed(
                task=str(fields.get("task") or ""),
                session=str(fields.get("session") or ""),
                status=str(fields.get("status") or ""),
                duration=int(fields.get("duration") or 0),
                summary=str(fields.get("summary") or ""),
            )
        except Exception:  # noqa: BLE001
            pass
    try:
        events_path = _sched._sched_config().events_file
    except Exception:
        return
    append_event(events_path, entry)


def _write_live_state(**fields) -> None:
    """Atomically write the live state JSON file.

    The writer's own PID is stamped on every write (#873). Liveness used to be
    inferred from ``tmux_session_exists(SCHEDULER_SESSION)``, which is false for
    a daemon supervised outside tmux (launchd), so a running daemon read as
    ``stopped`` and doctor skipped its staleness check exactly when it mattered.
    The PID is what the tmux gate was standing in for: something that says "the
    process that wrote this file is still alive", so a leftover file from a
    since-stopped daemon can't read as live. See :func:`live_daemon_state`.
    """
    from agentwire import scheduler as _sched

    fields["pid"] = os.getpid()
    try:
        live_path = _sched._sched_config().live_state_file
        live_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(live_path.parent), suffix=".tmp"
        )
        try:
            with open(fd, "w") as f:
                json.dump(fields, f, indent=2)
            Path(tmp_path).rename(live_path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
    except OSError:
        pass


def _notify_portal(task_name: str, status: str, duration: int, summary: str) -> None:
    """POST a scheduler_task_complete notification to the portal."""
    from agentwire import scheduler as _sched

    try:
        portal_request(
            "POST",
            f"{get_config().portal.url}/api/notify",
            json={
                "event": "scheduler_task_complete",
                "task": task_name,
                "status": status,
                "duration": duration,
                "summary": summary,
            },
            timeout=_sched._sched_config().portal_notify_timeout,
        )
    except Exception:
        pass  # Portal may not be running


def _notify_portal_state() -> None:
    """Push full scheduler live state to the portal via /api/notify."""
    from agentwire import scheduler as _sched

    try:
        state = read_live_state()
        if not state:
            return

        portal_request(
            "POST",
            f"{get_config().portal.url}/api/notify",
            json={"event": "scheduler_state", "running": True, **state},
            timeout=_sched._sched_config().portal_notify_timeout,
        )
    except Exception:
        pass  # Portal may not be running


def format_interval(seconds: int) -> str:
    """Format seconds into a human-readable interval string."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h{m}m" if m else f"{h}h"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d{h}h" if h else f"{d}d"


def format_overdue(seconds: float) -> str:
    """Format overdue seconds with +/- prefix."""
    prefix = "+" if seconds >= 0 else "-"
    abs_s = abs(int(seconds))
    return f"{prefix}{format_interval(abs_s)}"


def format_schedule(schedule: Schedule) -> str:
    """Format a Schedule into a human-readable string."""
    parts = []
    if schedule.every:
        parts.append(f"every {schedule.every}")
    if schedule.at:
        parts.append(f"at {schedule.at}")
    if schedule.after:
        parts.append(f"after {schedule.after}")
    if schedule.delay:
        parts.append(f"+{format_interval(schedule.delay)}")
    if schedule.cooldown:
        parts.append(f"cd {format_interval(schedule.cooldown)}")
    if schedule.except_days:
        parts.append(f"except {','.join(schedule.except_days)}")
    if schedule.not_before:
        parts.append(f">={schedule.not_before}")
    if schedule.not_after:
        parts.append(f"<={schedule.not_after}")
    return " ".join(parts) if parts else "?"


def get_board_display(board: Board) -> list[dict]:
    """Get board data formatted for display.

    Returns:
        List of dicts with task info and computed scores.
    """
    now = time.time()
    rows = []

    for name, task in board.tasks.items():
        state = board.state.get(name, TaskState())
        eligible_ts = _compute_next_eligible(board, name)
        if eligible_ts is not None:
            overdue_by = now - eligible_ts
        else:
            overdue_by = 0.0  # Blocked by dependency

        in_flight = _is_in_flight(state)

        # Format last run time
        if state.last_run:
            lr = state.last_run
            today = datetime.now().date()
            if lr.date() == today:
                last_run_str = lr.strftime("%H:%M")
            else:
                last_run_str = lr.strftime("%Y-%m-%d %H:%M")
        else:
            last_run_str = "never"

        label = name
        if task.filler:
            label = f"{name} (filler)"

        schedule_str = format_schedule(task.schedule)

        status_str = state.last_status
        if in_flight:
            status_str = "in-flight"

        row = {
            "name": name,
            "label": label,
            "schedule_str": schedule_str,
            "last_run": last_run_str,
            "last_run_iso": state.last_run.isoformat() if state.last_run else None,
            "last_status": status_str,
            "last_duration": state.last_duration,
            "run_count": state.run_count,
            "overdue_by": round(overdue_by, 1),
            "overdue_str": format_overdue(overdue_by),
            "enabled": task.enabled,
            "filler": task.filler,
            "priority": task.priority,
            "session": task.session,
            "task": task.task,
            "project": task.project,
            "in_flight": in_flight,
            "max_runs": task.max_runs,
            "once": task.once,
        }
        if state.last_summary:
            row["last_summary"] = state.last_summary
        if state.last_gate_error:
            row["last_gate_error"] = state.last_gate_error
        if state.last_gate_skip:
            row["last_gate_skip"] = state.last_gate_skip
        rows.append(row)

    # Sort: enabled first, then by overdue (most overdue first)
    rows.sort(key=lambda r: (not r["enabled"], -r["overdue_by"]))
    return rows


def read_events(tail: int = 20, task_filter: str | None = None) -> list[dict]:
    """Read recent events from the JSONL log.

    Args:
        tail: Number of most recent events to return.
        task_filter: Only return events for this task name.

    Returns:
        List of event dicts, most recent last.
    """
    from agentwire import scheduler as _sched

    events_path = _sched._sched_config().events_file
    if not events_path.exists():
        return []

    events = []
    try:
        with open(events_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    if task_filter and evt.get("task") != task_filter:
                        continue
                    events.append(evt)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    return events[-tail:]


def read_live_state() -> dict | None:
    """Read the live scheduler state.

    Returns:
        Live state dict or None if file doesn't exist.
    """
    from agentwire import scheduler as _sched

    live_path = _sched._sched_config().live_state_file
    if not live_path.exists():
        return None
    try:
        return json.loads(live_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _pid_is_scheduler(pid: int) -> bool:
    """Is *pid* a live process, and does it look like a scheduler daemon?

    Two checks, in order of cost:

    1. ``os.kill(pid, 0)`` — a pure syscall. ``ProcessLookupError`` means dead;
       ``PermissionError`` means alive but owned by someone else.
    2. The process's ``ps`` command line, TOKENISED, must contain both
       ``scheduler`` and ``serve`` as whole argv words.

    Step 2 exists because PIDs are recycled: an unrelated process inheriting a
    dead daemon's PID and reading as "the scheduler is running" would suppress
    the autostart guard and leave the board with NO dispatcher — worse than the
    bug this check fixes. A substring test is not enough for that, because the
    recycled process can easily be another agentwire command: ``agentwire
    scheduler live --watch`` contains ``scheduler`` but dispatches nothing, and
    would make ``status`` misreport AND ``serve`` / ``start`` / autostart all
    refuse. Only ``scheduler serve`` is a dispatcher, so require both words.

    ``ps`` unavailable or unreadable keeps the step-1 answer rather than
    reporting a live daemon dead.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if out.returncode != 0 or not out.stdout.strip():
        return True
    # Whole-word match on argv: a path component or a flag value that merely
    # contains "serve" must not qualify.
    argv = out.stdout.split()
    return "scheduler" in argv and "serve" in argv


def live_daemon_state() -> dict | None:
    """Live state of a VERIFIED-ALIVE scheduler daemon, else ``None`` (#873).

    The single source of truth for "is the scheduler daemon running". Every
    status surface (``scheduler status``, ``doctor``, the portal's autostart
    guard) routes through here instead of asking tmux, because tmux only knows
    about daemons it hosts — a launchd-supervised ``agentwire scheduler serve``
    is invisible to it, which both misreported liveness and let the portal
    autostart a SECOND dispatcher onto the same board.

    A state file with no ``pid`` was written by a daemon predating this change
    and cannot be verified, so it reads as not-running; restarting the daemon
    (which a rebuild already requires) heals it.
    """
    state = read_live_state()
    if not state:
        return None
    pid = state.get("pid")
    if not isinstance(pid, int):
        return None
    return state if _pid_is_scheduler(pid) else None
