"""Usage-limit dialog recovery — deterministic, zero-LLM.

When a Claude Code session hits its usage limit it parks on an interactive
dialog (``/rate-limit-options``) and blocks forever waiting for a human.
This module detects that dialog from pane text, selects "Stop and wait for
limit to reset", parses the reset time from the message, emails the owner,
and nudges the session back to work after the limit resets.

Every step is plain code: at the moment this fires, usage is exhausted by
definition — no agent can run to orchestrate the recovery, and a recovery
mechanism must be more reliable than the thing it recovers.

Detection runs in two places:

- ``agentwire limits tick`` — a stateless launchd watchdog (every 60s)
  sweeping all tmux panes. Also the resume timer: a tick that finds a parked
  session past its reset time sends the resume nudge.
- ensure's completion poll (``completion.wait_for_completion_signal``) —
  fast path (≤10s) for scheduler/overnight-dispatched tasks.

State: one JSON file per parked session under ``~/.agentwire/usage-limit/``
(worktree session names contain ``/`` and nest one directory down, same as
the tasks dir). File presence in the active dir == "parked" — that is the
guard ensure, the scheduler, the idle hook, and overnight all check so a
parked session is never prompted, re-dispatched, or reaped. Files are
archived to ``usage-limit/done/`` on resume.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path.home() / ".agentwire" / "usage-limit"
DONE_DIR = STATE_DIR / "done"
EVENTS_FILE = Path.home() / ".agentwire" / "usage-limit-events.jsonl"

# Owner-specified fixed resume message — the only agent involvement in the
# whole recovery story is the parked session acting on this nudge.
RESUME_NUDGE = (
    "You were interrupted by a usage limit; the limit has reset. "
    "Continue your task from where you stopped and complete it fully."
)

# The distinctive option line — anchor for "this is the usage-limit dialog".
PARK_OPTION = "Stop and wait for limit to reset"
# Present whenever a Claude Code select-menu is live on screen.
MENU_FOOTER = "Enter to confirm"
MENU_QUESTION = "What do you want to do?"

# Limits reset every 5h from window start, so now+5h is a guaranteed upper
# bound when the reset time can't be parsed from the dialog.
FALLBACK_RESET = timedelta(hours=5)
# A parsed reset further out than one window (+ slack) means the stated
# clock time already passed and rolled to tomorrow — i.e. reset is done.
MAX_WINDOW = timedelta(hours=5, minutes=15)
# Nudge this long after the stated reset so we're safely past it.
RESUME_BUFFER = timedelta(minutes=2)
# Give up nudging after this many failed attempts and archive as failed.
MAX_RESUME_ATTEMPTS = 5

# Shells that indicate no agent is running in a pane (mirror completion.py).
_BARE_SHELLS = {"zsh", "bash", "sh", "fish", "tcsh", "csh"}

# e.g. "You've hit your session limit · resets 11:40pm (America/Toronto)"
_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*\(([^)]+)\))?",
    re.IGNORECASE,
)


# =============================================================================
# Small utilities
# =============================================================================


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(text: str) -> str:
    """Collapse all whitespace so narrow-pane line wraps can't break matches."""
    return " ".join(text.split())


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def log_event(event: str, **fields) -> None:
    """Append an event to the usage-limit events log (best-effort)."""
    record = {"ts": _now().isoformat(), "event": event, **fields}
    try:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _tmux(args: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=timeout
    )


def _capture(target: str, scrollback: int | None = None) -> str:
    """Capture pane text. Visible screen only unless ``scrollback`` lines given."""
    cmd = ["capture-pane", "-t", target, "-p"]
    if scrollback:
        cmd += ["-S", f"-{scrollback}"]
    try:
        result = _tmux(cmd)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _session_exists(session: str) -> bool:
    try:
        return _tmux(["has-session", "-t", f"={session}"]).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _send_key(target: str, key: str) -> None:
    _tmux(["send-keys", "-t", target, key])


# =============================================================================
# State files
# =============================================================================


def state_path(session: str) -> Path:
    return STATE_DIR / f"{session}.json"


def is_parked(session: str) -> bool:
    """True iff this session is currently parked on a usage limit."""
    return state_path(session).exists()


def read_park_state(session: str) -> dict | None:
    try:
        return json.loads(state_path(session).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_park_state(state: dict) -> None:
    _atomic_write(state_path(state["session"]), state)


def list_parked() -> list[dict]:
    """All active park states (excludes the done/ archive)."""
    if not STATE_DIR.exists():
        return []
    states = []
    for path in sorted(STATE_DIR.rglob("*.json")):
        if DONE_DIR in path.parents:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("session"):
            states.append(data)
    return states


def archive_state(state: dict, status: str) -> None:
    """Move a park state into done/ with a final status."""
    state["status"] = status
    state["archived_at"] = _now().isoformat()
    flat = state["session"].replace("/", "_")
    ts = _now().strftime("%Y%m%dT%H%M%S")
    _atomic_write(DONE_DIR / f"{flat}-{ts}.json", state)
    try:
        state_path(state["session"]).unlink(missing_ok=True)
    except OSError:
        pass


# =============================================================================
# Detection
# =============================================================================


def detect_dialog(visible: str) -> bool:
    """True iff the usage-limit dialog is live on this (visible) screen.

    Requires the distinctive park option AND the menu footer, and nothing
    rendered after the menu — a pane merely *displaying* a captured dialog
    (an orchestrator reviewing another session's output) has its own prompt
    box below the quoted text and must not be parked.
    """
    norm = _normalize(visible)
    if PARK_OPTION not in norm or MENU_FOOTER not in norm:
        return False
    tail = norm.rsplit(MENU_FOOTER, 1)[1]
    # A live menu ends the screen: "Enter to confirm · Esc to cancel".
    tail = tail.replace("·", " ").replace("Esc to cancel", " ")
    return not tail.strip()


def detect_dialog_like(visible: str) -> bool:
    """A live select-menu that is NOT the known usage-limit dialog.

    Used to log dialog-text drift across Claude Code versions — never acted
    on, only surfaced via ``unmatched_dialog`` events.
    """
    norm = _normalize(visible)
    if MENU_QUESTION not in norm or MENU_FOOTER not in norm:
        return False
    return not detect_dialog(visible)


def parse_reset_time(text: str, now: datetime | None = None) -> datetime | None:
    """Parse the limit reset time from dialog/scrollback text.

    Matches e.g. "resets 11:40pm (America/Toronto)" — last occurrence wins
    (the freshest message is lowest in the scrollback). Returns an aware UTC
    datetime, ``now`` if the stated time already passed (reset is done), or
    None if nothing parseable is found.
    """
    now = now or _now()
    matches = list(_RESET_RE.finditer(_normalize(text)))
    if not matches:
        return None
    hour_s, minute_s, meridiem, tz_name = matches[-1].groups()

    hour = int(hour_s) % 12
    if meridiem.lower() == "pm":
        hour += 12
    minute = int(minute_s) if minute_s else 0
    if hour > 23 or minute > 59:
        return None

    tzinfo = None
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            tzinfo = ZoneInfo(tz_name.strip())
        except Exception:
            tzinfo = None
    if tzinfo is None:
        tzinfo = datetime.now().astimezone().tzinfo

    local_now = now.astimezone(tzinfo)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    if candidate - local_now > MAX_WINDOW:
        # Stated clock time already passed today — the reset has happened.
        return now
    return candidate.astimezone(timezone.utc)


# =============================================================================
# Park
# =============================================================================


def _task_info(session: str) -> dict:
    """Best-effort context for notifications: task name + project path."""
    info: dict = {}
    task_file = Path.home() / ".agentwire" / "tasks" / f"{session}.json"
    try:
        ctx = json.loads(task_file.read_text())
        if isinstance(ctx, dict) and ctx.get("task"):
            info["task"] = ctx["task"]
    except (OSError, json.JSONDecodeError):
        pass
    try:
        result = _tmux(["display-message", "-p", "-t", session, "#{pane_current_path}"])
        if result.returncode == 0 and result.stdout.strip():
            info["project_path"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return info


def park(session: str, pane_index: int = 0, source: str = "watchdog") -> dict | None:
    """Park a session sitting on the usage-limit dialog.

    Selects option 1 ("Stop and wait for limit to reset"), parses the reset
    time, writes the park state file, and sends the owner notification.
    Idempotent: an already-parked session is a no-op. Returns the park state,
    or None if the session wasn't actually on the dialog.
    """
    if is_parked(session):
        return None

    target = f"{session}.{pane_index}"
    visible = _capture(target)
    if not detect_dialog(visible):
        return None

    now = _now()
    scrollback = _capture(target, scrollback=300)
    reset_at = parse_reset_time(scrollback or visible, now)
    parse_failed = reset_at is None
    if parse_failed:
        reset_at = now + FALLBACK_RESET
        log_event(
            "reset_parse_failed", session=session, pane=pane_index,
            excerpt=_normalize(visible)[-500:],
        )

    # Select option 1 and confirm; verify the menu actually dismissed.
    for attempt in range(2):
        _send_key(target, "1")
        time.sleep(0.3)
        _send_key(target, "Enter")
        time.sleep(1.0)
        if not detect_dialog(_capture(target)):
            break
        if attempt == 1:
            log_event("park_confirm_failed", session=session, pane=pane_index)

    state = {
        "session": session,
        "pane": pane_index,
        "status": "parked",
        "source": source,
        "detected_at": now.isoformat(),
        "parked_at": _now().isoformat(),
        "reset_at": reset_at.isoformat(),
        "resume_at": (reset_at + RESUME_BUFFER).isoformat(),
        "reset_parse_failed": parse_failed,
        "notified": False,
        "resume_attempts": 0,
        "excerpt": _normalize(visible)[-500:],
        **_task_info(session),
    }
    write_park_state(state)
    log_event(
        "session_parked", session=session, pane=pane_index, source=source,
        reset_at=state["reset_at"], resume_at=state["resume_at"],
        task=state.get("task"),
    )
    _notify_parked(state)
    return state


def _fmt_local(iso: str) -> str:
    try:
        return (
            datetime.fromisoformat(iso)
            .astimezone()
            .strftime("%Y-%m-%d %I:%M%p %Z")
        )
    except ValueError:
        return iso


def _notify_parked(state: dict) -> bool:
    """Email the owner that a session is parked. Plain Resend call, no agent."""
    session = state["session"]
    lines = [
        f"Session **{session}** on `{socket.gethostname()}` hit a usage limit "
        "and was parked (option 1: stop and wait for limit to reset).",
        "",
        f"- **Task:** {state.get('task') or '(none — not a tracked task)'}",
        f"- **Project:** {state.get('project_path') or 'unknown'}",
        f"- **Detected:** {_fmt_local(state['detected_at'])}",
        f"- **Limit resets:** {_fmt_local(state['reset_at'])}"
        + (" (unparsed — assumed +5h window)" if state.get("reset_parse_failed") else ""),
        f"- **Auto-resume:** {_fmt_local(state['resume_at'])}",
        "",
        "The session will be nudged automatically after reset — no action needed.",
        "",
        "```",
        state.get("excerpt", ""),
        "```",
    ]
    return _send_notification(
        state,
        subject=f"[agentwire] usage limit: {session} parked until {_fmt_local(state['reset_at'])}",
        body="\n".join(lines),
        mark_notified=True,
    )


def _notify_resumed(state: dict) -> None:
    session = state["session"]
    _send_notification(
        state,
        subject=f"[agentwire] usage limit reset: {session} resumed",
        body=(
            f"Session **{session}** on `{socket.gethostname()}` was nudged to "
            f"continue after its usage limit reset.\n\n"
            f"- **Task:** {state.get('task') or '(none)'}\n"
            f"- **Parked:** {_fmt_local(state['parked_at'])}\n"
            f"- **Resumed:** {_fmt_local(_now().isoformat())}"
        ),
        mark_notified=False,
    )


def _send_notification(state: dict, subject: str, body: str, mark_notified: bool) -> bool:
    try:
        from .channels.email import send_email

        result = send_email(subject=subject, body=body)
        if result.success:
            if mark_notified:
                state["notified"] = True
                if is_parked(state["session"]):
                    write_park_state(state)
            log_event("notify_sent", session=state["session"], subject=subject)
            return True
        log_event("notify_failed", session=state["session"], error=result.error)
    except Exception as e:
        log_event("notify_failed", session=state["session"], error=str(e))
    return False


# =============================================================================
# Sweep (detection backstop across every tmux pane)
# =============================================================================


def sweep() -> list[dict]:
    """Scan all tmux panes for the usage-limit dialog; park what's found."""
    try:
        result = _tmux(
            ["list-panes", "-a", "-F",
             "#{session_name}\t#{pane_index}\t#{pane_current_command}"]
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []

    parked = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        session, pane_s, command = parts
        if command.strip().lower() in _BARE_SHELLS:
            continue
        if is_parked(session):
            continue
        try:
            pane_index = int(pane_s)
        except ValueError:
            continue

        visible = _capture(f"{session}.{pane_index}")
        if detect_dialog(visible):
            state = park(session, pane_index, source="watchdog")
            if state:
                parked.append(state)
        elif detect_dialog_like(visible):
            _log_unmatched(session, pane_index, visible)
    return parked


def _log_unmatched(session: str, pane_index: int, visible: str) -> None:
    """Log a dialog-like screen we don't recognize — once per distinct screen."""
    excerpt = _normalize(visible)[-500:]
    marker_file = STATE_DIR / ".unmatched.json"
    try:
        seen = json.loads(marker_file.read_text())
    except (OSError, json.JSONDecodeError):
        seen = {}
    if not isinstance(seen, dict):
        seen = {}
    key = f"{session}.{pane_index}"
    digest = str(hash(excerpt))
    if seen.get(key) == digest:
        return
    seen[key] = digest
    _atomic_write(marker_file, seen)
    log_event("unmatched_dialog", session=session, pane=pane_index, excerpt=excerpt)


# =============================================================================
# Resume
# =============================================================================


def _nudge_visible(target: str) -> bool:
    norm = _normalize(_capture(target))
    return "interrupted by a usage limit" in norm or "[Pasted text" in norm


def resume_session(state: dict, force: bool = False) -> bool:
    """Send the resume nudge to a parked session; archive state on success."""
    session = state["session"]
    pane = state.get("pane", 0)
    target = f"{session}.{pane}"

    if not _session_exists(session):
        archive_state(state, "orphaned")
        log_event("park_orphaned", session=session)
        return False

    from . import pane_manager

    attempts = state.get("resume_attempts", 0)
    try:
        pane_manager.send_to_target(target, RESUME_NUDGE, enter=True)
        time.sleep(2.0)
        delivered = _nudge_visible(target)
    except Exception as e:
        log_event("resume_send_error", session=session, error=str(e))
        delivered = False

    if delivered or force:
        state["resumed_at"] = _now().isoformat()
        archive_state(state, "resumed")
        log_event("session_resumed", session=session, attempts=attempts + 1,
                  forced=bool(force and not delivered))
        _notify_resumed(state)
        return True

    state["resume_attempts"] = attempts + 1
    if state["resume_attempts"] >= MAX_RESUME_ATTEMPTS:
        archive_state(state, "resume_failed")
        log_event("resume_failed", session=session, attempts=state["resume_attempts"])
        _send_notification(
            state,
            subject=f"[agentwire] usage limit: FAILED to resume {session}",
            body=(
                f"Session **{session}** could not be nudged after "
                f"{state['resume_attempts']} attempts — it needs a human look."
            ),
            mark_notified=False,
        )
    else:
        write_park_state(state)
        log_event("resume_retry", session=session, attempts=state["resume_attempts"])
    return False


def resume_due(now: datetime | None = None) -> list[str]:
    """Resume every parked session whose reset (+ buffer) has passed."""
    now = now or _now()
    resumed = []
    for state in list_parked():
        session = state["session"]
        if not _session_exists(session):
            archive_state(state, "orphaned")
            log_event("park_orphaned", session=session)
            continue
        if not state.get("notified"):
            _notify_parked(state)
        try:
            due = datetime.fromisoformat(state["resume_at"]) <= now
        except (KeyError, ValueError):
            due = True
        if due and resume_session(state):
            resumed.append(session)
    return resumed


# =============================================================================
# Tick (the stateless watchdog entry point)
# =============================================================================


def tick() -> dict:
    """One watchdog pass: sweep for dialogs, resume what's due.

    Stateless and self-contained — safe to run from launchd every minute.
    A non-blocking lock skips overlapping ticks.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = open(STATE_DIR / ".tick.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return {"skipped": "tick already running"}

    try:
        parked = sweep()
        resumed = resume_due()
        return {
            "parked": [s["session"] for s in parked],
            "resumed": resumed,
            "waiting": [s["session"] for s in list_parked()],
        }
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
