"""Scheduler dataclasses, constants, and pure parsing helpers."""

import re
from dataclasses import dataclass, field
from datetime import datetime

from ..config import get_config


def _sched_config():
    """Get scheduler config section."""
    return get_config().scheduler


# tmux session name for the scheduler daemon
SCHEDULER_SESSION = "agentwire-scheduler"

# Exit codes from ensure (must match __main__.py constants)
_EXIT_COMPLETE = 0
_EXIT_FAILED = 1
_EXIT_INCOMPLETE = 2
_EXIT_LOCK_CONFLICT = 3
_EXIT_PRE_FAILURE = 4
_EXIT_TIMEOUT = 5
_EXIT_SESSION_ERROR = 6
_EXIT_USAGE_LIMIT = 7

_EXIT_TO_STATUS = {
    _EXIT_COMPLETE: "complete",
    _EXIT_FAILED: "failed",
    _EXIT_INCOMPLETE: "incomplete",
    _EXIT_LOCK_CONFLICT: "lock_conflict",
    _EXIT_PRE_FAILURE: "failed",
    _EXIT_TIMEOUT: "timeout",
    _EXIT_SESSION_ERROR: "failed",
    _EXIT_USAGE_LIMIT: "usage_limit",
}

# In-flight grace period: tasks dispatched less than 2h ago are considered running
_IN_FLIGHT_GRACE = 7200

# Day name constants for schedule matching
_DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_CALENDAR_EVERY = {"day", "weekday", "weekend"} | set(_DAY_NAMES)


@dataclass
class Schedule:
    every: str | None = None          # "2h", "day", "weekday", "monday", etc.
    at: str | None = None             # "HH:MM"
    except_days: list[str] | None = None
    after: str | None = None          # dependency task name
    delay: int = 0                    # seconds after dependency
    cooldown: int | None = None       # min seconds between runs
    require_status: str = "complete"
    not_before: str | None = None     # "HH:MM"
    not_after: str | None = None      # "HH:MM"


@dataclass
class SchedulerTask:
    name: str
    project: str = ""     # ~/projects/foo (expanded at load time)
    session: str = ""     # tmux session name for ensure dispatch
    task: str = ""        # task name in project's .agentwire.yml
    schedule: Schedule = field(default_factory=Schedule)  # REQUIRED (replaces interval)
    enabled: bool = True
    filler: bool = False  # only runs in spare cycles
    priority: int = 99    # task ordering (lower = higher priority)
    posture: str | None = None  # posture override (e.g., bypass, auto)
    roles: list[str] | None = None  # role override
    model: str | None = None  # model override
    gate: dict | None = None  # precondition gate (git_commit, git_diff, command)
    max_runs: int | None = None  # auto-disable after N successful dispatches
    once: bool = False           # shorthand for max_runs: 1
    # Worktree+PR mode: run in an isolated worktree off `base`, open a draft PR
    # back to `pr_target`. None = auto (on when `project` is a git repo).
    worktree: bool | None = None
    base: str = "main"           # branch the worktree forks from (fetched fresh)
    pr_target: str | None = None  # PR base branch (defaults to `base`)
    pr_draft: bool = True        # open the PR as a draft


@dataclass
class TaskState:
    last_run: datetime | None = None
    last_status: str = "never"    # complete, failed, incomplete, timeout, lock_conflict, usage_limit, never
    last_duration: int = 0
    run_count: int = 0
    last_summary: str = ""
    last_gate_error: str = ""     # last gate-eval exception reason (failed open)
    last_gate_commit: str = ""    # HEAD at last dispatch (for gate checks)
    last_dispatch: datetime | None = None  # set BEFORE running (restart safety)
    # Active worktree-PR tracking (set on finalize, cleared by the reaper on
    # PR merge/close). Lets the daemon reap the worktree + branch + session.
    worktree_branch: str = ""
    worktree_path: str = ""
    worktree_session: str = ""
    pr_number: int | None = None
    pr_url: str = ""


@dataclass
class Board:
    tasks: dict[str, SchedulerTask] = field(default_factory=dict)
    state: dict[str, TaskState] = field(default_factory=dict)


def _parse_duration(s: str | None) -> int | None:
    """Parse a duration string like '2h', '30m', '1d' to seconds.

    Returns None if input is None or unparseable.
    """
    if not s:
        return None
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(s|m|h|d)", s)
    if not m:
        # Try bare integer (seconds)
        try:
            return int(s)
        except ValueError:
            return None
    value, unit = int(m.group(1)), m.group(2)
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _parse_time(s: str | None) -> tuple[int, int] | None:
    """Parse 'HH:MM' to (hour, minute). Returns None on failure."""
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _parse_schedule(raw: dict | None) -> Schedule:
    """Parse a schedule dict from YAML into a Schedule dataclass."""
    if not raw or not isinstance(raw, dict):
        raise ValueError("task missing required 'schedule' field")

    except_days = raw.get("except")
    if isinstance(except_days, str):
        except_days = [except_days]
    if except_days:
        except_days = [d.strip().lower() for d in except_days]

    delay_seconds = _parse_duration(str(raw["delay"])) if raw.get("delay") else 0
    cooldown_seconds = _parse_duration(str(raw["cooldown"])) if raw.get("cooldown") else None

    return Schedule(
        every=raw.get("every"),
        at=raw.get("at"),
        except_days=except_days,
        after=raw.get("after"),
        delay=delay_seconds or 0,
        cooldown=cooldown_seconds,
        require_status=str(raw.get("require_status", "complete")),
        not_before=raw.get("not_before"),
        not_after=raw.get("not_after"),
    )


def _parse_datetime_field(raw_value) -> datetime | None:
    """Parse a datetime from YAML (handles datetime objects and ISO strings)."""
    if not raw_value:
        return None
    if isinstance(raw_value, datetime):
        return raw_value
    try:
        return datetime.fromisoformat(str(raw_value))
    except (ValueError, TypeError):
        return None


def _ts() -> str:
    """Current timestamp for log lines."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
