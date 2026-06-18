"""Centralized task scheduler daemon.

Reads a board of registered tasks from ~/.agentwire/scheduler.yaml,
picks the most overdue one, dispatches it via `agentwire ensure`,
updates the board, and loops. No AI — pure subprocess management
and time math.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import get_config


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
    type: str | None = None  # session type override (e.g., claude-bypass)
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


def load_board() -> Board:
    """Load the scheduler board from YAML.

    Returns:
        Board with tasks and state populated.

    Raises:
        FileNotFoundError: If board file doesn't exist.
        ValueError: If board file is malformed.
    """
    board_path = _sched_config().board_file
    if not board_path.exists():
        raise FileNotFoundError(
            f"Board file not found: {board_path}\n"
            f"Create it with task definitions."
        )

    with open(board_path) as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        raise ValueError(f"Board file is empty or malformed: {board_path}")

    raw_tasks = raw.get("tasks", {})
    if not raw_tasks:
        raise ValueError(f"No tasks defined in board: {board_path}")

    board = Board()

    for name, t in raw_tasks.items():
        if not isinstance(t, dict):
            continue
        raw_roles = t.get("roles")
        if isinstance(raw_roles, list):
            roles = [str(r) for r in raw_roles]
        elif isinstance(raw_roles, str):
            roles = [r.strip() for r in raw_roles.split(",") if r.strip()]
        else:
            roles = None

        schedule = _parse_schedule(t.get("schedule"))

        raw_project = t.get("project", "")
        project_path = str(Path(raw_project).expanduser()) if raw_project else ""

        session_name = str(t.get("session") or name)
        task_name = str(t.get("task") or name)

        board.tasks[name] = SchedulerTask(
            name=name,
            project=project_path,
            session=session_name,
            task=task_name,
            schedule=schedule,
            enabled=bool(t.get("enabled", True)),
            filler=bool(t.get("filler", False)),
            priority=int(t.get("priority", 99)),
            type=t.get("type"),
            roles=roles,
            model=t.get("model"),
            gate=t.get("gate"),
            max_runs=int(t["max_runs"]) if t.get("max_runs") is not None else None,
            once=bool(t.get("once", False)),
            worktree=t.get("worktree"),  # None = auto-detect (git repo)
            base=str(t.get("base", "main")),
            pr_target=t.get("pr_target"),
            pr_draft=bool(t.get("pr_draft", True)),
        )
        # Normalize: once: true is shorthand for max_runs: 1
        st = board.tasks[name]
        if st.once and st.max_runs is None:
            st.max_runs = 1

    raw_state = raw.get("state", {})
    if raw_state and isinstance(raw_state, dict):
        for name, s in raw_state.items():
            if not isinstance(s, dict):
                continue
            board.state[name] = TaskState(
                last_run=_parse_datetime_field(s.get("last_run")),
                last_status=str(s.get("last_status", "never")),
                last_duration=int(s.get("last_duration", 0)),
                run_count=int(s.get("run_count", 0)),
                last_summary=str(s.get("last_summary", "")),
                last_gate_error=str(s.get("last_gate_error", "")),
                last_gate_commit=str(s.get("last_gate_commit", "")),
                last_dispatch=_parse_datetime_field(s.get("last_dispatch")),
                worktree_branch=str(s.get("worktree_branch", "")),
                worktree_path=str(s.get("worktree_path", "")),
                worktree_session=str(s.get("worktree_session", "")),
                pr_number=int(s["pr_number"]) if s.get("pr_number") is not None else None,
                pr_url=str(s.get("pr_url", "")),
            )

    return board


def save_board(board: Board) -> None:
    """Save board state back to YAML (preserves task definitions, rewrites state)."""
    board_path = _sched_config().board_file
    if not board_path.exists():
        return

    with open(board_path) as f:
        raw = yaml.safe_load(f) or {}

    # Only update the state section
    state_dict = {}
    for name, s in board.state.items():
        entry = {
            "last_run": s.last_run.isoformat() if s.last_run else None,
            "last_status": s.last_status,
            "last_duration": s.last_duration,
            "run_count": s.run_count,
        }
        if s.last_summary:
            entry["last_summary"] = s.last_summary
        if s.last_gate_error:
            entry["last_gate_error"] = s.last_gate_error
        if s.last_gate_commit:
            entry["last_gate_commit"] = s.last_gate_commit
        if s.last_dispatch:
            entry["last_dispatch"] = s.last_dispatch.isoformat()
        if s.worktree_branch:
            entry["worktree_branch"] = s.worktree_branch
        if s.worktree_path:
            entry["worktree_path"] = s.worktree_path
        if s.worktree_session:
            entry["worktree_session"] = s.worktree_session
        if s.pr_number is not None:
            entry["pr_number"] = s.pr_number
        if s.pr_url:
            entry["pr_url"] = s.pr_url
        state_dict[name] = entry

    raw["state"] = state_dict

    with open(board_path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)


def _dt_to_ts(dt: datetime | None) -> float:
    """Convert a datetime to a Unix timestamp (0 if None)."""
    if not dt:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _get_last_run_ts(board: Board, task_name: str) -> float:
    """Get effective last run as a Unix timestamp (0 if never run).

    Returns max(last_run, last_dispatch) for restart safety — a recently
    dispatched task should not be re-dispatched even if we crash before
    recording last_run.
    """
    state = board.state.get(task_name)
    if not state:
        return 0.0
    return max(_dt_to_ts(state.last_run), _dt_to_ts(state.last_dispatch))


def _is_in_flight(state: TaskState | None) -> bool:
    """Return True if a task was dispatched recently but hasn't completed.

    Uses a 2h grace period — if last_dispatch is more recent than last_run
    and within 2h, the task is still considered running.
    """
    if not state or not state.last_dispatch:
        return False
    dispatch_ts = _dt_to_ts(state.last_dispatch)
    run_ts = _dt_to_ts(state.last_run)
    if run_ts >= dispatch_ts:
        return False  # Completed after dispatch
    now = time.time()
    return (now - dispatch_ts) < _IN_FLIGHT_GRACE


def _day_matches(dt: datetime, every: str | None, except_days: list[str] | None) -> bool:
    """Check if a datetime matches day-of-week constraints.

    Returns True if the day is allowed by `every` and not excluded by `except_days`.
    """
    day_name = _DAY_NAMES[dt.weekday()]

    # Check except_days first
    if except_days and day_name in except_days:
        return False

    if not every or every in ("day",):
        return True
    if every == "weekday":
        return dt.weekday() < 5
    if every == "weekend":
        return dt.weekday() >= 5
    if every in _DAY_NAMES:
        return day_name == every

    # Duration-based every (like "4h") — day check only via except_days
    return True


def _in_time_window(schedule: Schedule) -> bool:
    """Check if current local time is within not_before/not_after window."""
    now_local = datetime.now()
    current_minutes = now_local.hour * 60 + now_local.minute

    nb = _parse_time(schedule.not_before)
    if nb:
        nb_minutes = nb[0] * 60 + nb[1]
        if current_minutes < nb_minutes:
            return False

    na = _parse_time(schedule.not_after)
    if na:
        na_minutes = na[0] * 60 + na[1]
        if current_minutes > na_minutes:
            return False

    return True


def _compute_recurrence(schedule: Schedule, last_run_ts: float) -> float:
    """Compute next eligible timestamp from the recurrence rule (every + at).

    Returns a Unix timestamp of when the task becomes eligible.
    """
    now = time.time()
    every = schedule.every

    if not every:
        # No recurrence — only dependency-driven. Eligible immediately if never run.
        return last_run_ts if last_run_ts > 0 else 0.0

    # Duration-based every (e.g., "2h", "30m")
    duration = _parse_duration(every)
    if duration is not None:
        if last_run_ts == 0:
            return 0.0  # Never run, eligible now
        return last_run_ts + duration

    # Calendar-based every (day, weekday, weekend, monday..sunday)
    at_time = _parse_time(schedule.at)
    if not at_time:
        # Calendar without 'at' — treat like 24h interval
        if last_run_ts == 0:
            return 0.0
        return last_run_ts + 86400

    target_h, target_m = at_time
    now_local = datetime.now()

    # Find today's target time
    import calendar
    from datetime import timedelta
    target_today = now_local.replace(hour=target_h, minute=target_m, second=0, microsecond=0)

    # Search forward up to 8 days for the next matching day
    for day_offset in range(8):
        candidate = target_today + timedelta(days=day_offset)
        if _day_matches(candidate, every, schedule.except_days):
            candidate_ts = candidate.timestamp()
            if candidate_ts > last_run_ts:
                return candidate_ts

    # Fallback: 24h from last run
    return last_run_ts + 86400 if last_run_ts > 0 else 0.0


def _compute_next_eligible(board: Board, task_name: str) -> float | None:
    """Central scheduling logic. Compute when a task becomes eligible.

    Returns:
        Unix timestamp of next eligible time, or None if blocked indefinitely.
    """
    task = board.tasks[task_name]
    schedule = task.schedule
    state = board.state.get(task_name, TaskState())
    last_run_ts = _get_last_run_ts(board, task_name)

    # Start with recurrence-based eligibility
    eligible_ts = _compute_recurrence(schedule, last_run_ts)

    # Dependency: after another task
    if schedule.after:
        dep_name = schedule.after
        dep_state = board.state.get(dep_name)

        if not dep_state or not dep_state.last_run:
            return None  # Dependency never ran — blocked

        # Check require_status
        if schedule.require_status == "complete" and dep_state.last_status != "complete":
            return None  # Dependency didn't complete successfully

        dep_run_ts = _dt_to_ts(dep_state.last_run)

        # Dependency must have completed more recently than this task last ran
        if dep_run_ts <= last_run_ts and last_run_ts > 0:
            return None  # No new dependency completion

        # Apply delay after dependency completion
        dep_eligible = dep_run_ts + schedule.delay
        eligible_ts = max(eligible_ts, dep_eligible)

    # Cooldown: minimum time between runs
    if schedule.cooldown and last_run_ts > 0:
        cooldown_eligible = last_run_ts + schedule.cooldown
        eligible_ts = max(eligible_ts, cooldown_eligible)

    return eligible_ts


_gated_tasks: set[str] = set()
"""Tracks tasks already reported as gated to avoid log spam.

Cleared per-task when the task is dispatched (runs) or when
conditions change (new commits make the gate pass).
"""

_gate_errored: dict[str, str] = {}
"""Last gate-eval error reason per task (log-spam control + change detection).

Cleared when the gate next evaluates cleanly, so a transient git timeout
surfaces once rather than on every loop.
"""


def _check_gate(board: Board, task_name: str) -> bool:
    """Return True if task should run, False to skip.

    Evaluates gate preconditions defined on the task. Multiple gate keys
    are AND'd — all must pass. Fails OPEN (returns True) on errors,
    missing baseline, or no gate defined — but a gate-eval *exception* is
    no longer silent: it's logged to the event stream and surfaced on the
    board as `last_gate_error`, so a git timeout that lets the task run
    anyway leaves a trail instead of vanishing.

    Only logs the first time a task is gated — subsequent checks for the
    same task are silent until the task runs or conditions change.
    """
    task = board.tasks[task_name]
    gate = task.gate
    if not gate or not isinstance(gate, dict):
        _gated_tasks.discard(task_name)
        _clear_gate_error(board, task_name)
        return True

    cfg = _sched_config()
    state = board.state.get(task_name, TaskState())
    project = task.project

    def _gate_skip(gate_type: str, reason: str, **extra):
        """Record a gate skip, only logging on first occurrence."""
        if task_name not in _gated_tasks:
            _log_event("task_gated", task=task_name, gate_type=gate_type,
                       reason=reason, **extra)
            print(f"[{_ts()}] Skipping {task_name}: gate {gate_type} ({reason})")
            _gated_tasks.add(task_name)
        return False

    def _gate_error(gate_type: str, exc: Exception):
        """Record a gate-eval exception, then fail OPEN.

        Deliberately keeps the fail-open behaviour (a closed gate that
        errored would block every task) but routes the captured reason
        into the event log + board so the run is no longer UNLOGGED.
        """
        reason = " ".join(f"{type(exc).__name__}: {exc}".split())[:200]
        if _gate_errored.get(task_name) != reason:
            _gate_errored[task_name] = reason
            _log_event("gate_error", task=task_name, gate_type=gate_type,
                       reason=reason)
            print(f"[{_ts()}] Gate error on {task_name}: {gate_type} "
                  f"({reason}) — failing open")
            state.last_gate_error = f"{gate_type}: {reason}"
            board.state[task_name] = state
            save_board(board)
        _gated_tasks.discard(task_name)
        return True

    # git_commit: skip if HEAD unchanged since last run
    if gate.get("git_commit"):
        if not state.last_gate_commit:
            _gated_tasks.discard(task_name)
            return True  # No baseline, first run
        try:
            result = subprocess.run(
                ["git", "-C", project, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=cfg.git_timeout,
            )
            if result.returncode == 0:
                current_head = result.stdout.strip()
                if current_head == state.last_gate_commit:
                    return _gate_skip("git_commit", "no new commits")
        except Exception as exc:
            return _gate_error("git_commit", exc)

    # git_diff: skip if no commits touched matching paths
    git_diff_paths = gate.get("git_diff")
    if git_diff_paths and isinstance(git_diff_paths, list):
        if not state.last_gate_commit:
            _gated_tasks.discard(task_name)
            return True  # No baseline, first run
        try:
            cmd = ["git", "-C", project, "diff", "--name-only",
                   f"{state.last_gate_commit}..HEAD", "--"]
            cmd.extend(str(p) for p in git_diff_paths)
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=cfg.git_timeout,
            )
            if result.returncode == 0 and not result.stdout.strip():
                return _gate_skip("git_diff", f"no changes in {', '.join(git_diff_paths)}",
                                  paths=git_diff_paths)
        except Exception as exc:
            return _gate_error("git_diff", exc)

    # command: skip if command exits non-zero
    gate_cmd = gate.get("command")
    if gate_cmd and isinstance(gate_cmd, str):
        try:
            result = subprocess.run(
                gate_cmd, shell=True, cwd=project,
                capture_output=True, timeout=cfg.gate_timeout,
            )
            if result.returncode != 0:
                return _gate_skip("command", f"exit {result.returncode}",
                                  command=gate_cmd)
        except Exception as exc:
            return _gate_error("command", exc)

    # Gate passed — clear from gated set so it can be re-reported if gated again later
    _gated_tasks.discard(task_name)
    _clear_gate_error(board, task_name)
    return True


def _clear_gate_error(board: Board, task_name: str) -> None:
    """Clear a previously-recorded gate error once the gate evaluates cleanly.

    No-op (and no board write) unless this task actually had an error, so
    the common clean path stays cheap.
    """
    state = board.state.get(task_name)
    had_tracked = _gate_errored.pop(task_name, None) is not None
    if had_tracked or (state is not None and state.last_gate_error):
        if state is not None and state.last_gate_error:
            state.last_gate_error = ""
            save_board(board)


def pick_next_task(board: Board) -> tuple[str | None, float]:
    """Pick the next task to run based on schedule eligibility, priority, and overdue score.

    Algorithm:
    1. For each enabled non-filler task, compute eligible_ts via _compute_next_eligible()
    2. Skip if in-flight, outside time window, or day excluded
    3. Sort by (priority, -overdue_by), pick first passing gate
    4. Same for fillers
    5. If nothing due, return sleep time

    Returns:
        (task_name, wait_seconds) — task_name is None if nothing to do,
        wait_seconds is 0 if task should run now, >0 if should wait.
    """
    now = time.time()

    # Collect overdue non-filler candidates
    candidates: list[tuple[str, float]] = []
    for name, task in board.tasks.items():
        if not task.enabled or task.filler:
            continue
        state = board.state.get(name, TaskState())
        if task.max_runs is not None and state.run_count >= task.max_runs:
            continue  # Hit run limit
        if _is_in_flight(state):
            continue
        if not _in_time_window(task.schedule):
            continue
        if not _day_matches(datetime.now(), task.schedule.every, task.schedule.except_days):
            continue
        eligible_ts = _compute_next_eligible(board, name)
        if eligible_ts is None:
            continue  # Blocked by dependency
        overdue_by = now - eligible_ts
        if overdue_by >= 0:
            candidates.append((name, overdue_by))
    candidates.sort(key=lambda x: (board.tasks[x[0]].priority, -x[1]))

    # Pick the first overdue candidate that passes its gate
    for name, _score in candidates:
        if _check_gate(board, name):
            return name, 0.0

    # No non-filler passed gate — check fillers
    filler_candidates: list[tuple[str, float]] = []
    for name, task in board.tasks.items():
        if not task.filler or not task.enabled:
            continue
        state = board.state.get(name)
        if _is_in_flight(state):
            continue
        if not _in_time_window(task.schedule):
            continue
        if not _day_matches(datetime.now(), task.schedule.every, task.schedule.except_days):
            continue
        eligible_ts = _compute_next_eligible(board, name)
        if eligible_ts is None:
            continue
        overdue_by = now - eligible_ts
        if overdue_by >= 0:
            filler_candidates.append((name, overdue_by))
    filler_candidates.sort(key=lambda x: (board.tasks[x[0]].priority, -x[1]))

    for name, _score in filler_candidates:
        if _check_gate(board, name):
            return name, 0.0

    # Nothing to run now — calculate sleep until earliest task is due
    wait = seconds_until_next_due(board)
    return None, wait


def seconds_until_next_due(board: Board) -> float:
    """Calculate seconds until the earliest task is due.

    Returns:
        Seconds to wait (0 if something is already due, 60.0 max as fallback).
    """
    now = time.time()
    earliest_wait = float("inf")

    for name, task in board.tasks.items():
        if not task.enabled:
            continue
        eligible_ts = _compute_next_eligible(board, name)
        if eligible_ts is None:
            continue  # Blocked by dependency, skip
        wait = eligible_ts - now
        if wait <= 0:
            return 0.0
        if wait < earliest_wait:
            earliest_wait = wait

    if earliest_wait == float("inf"):
        return float(_sched_config().max_loop_sleep)

    return earliest_wait


def _log_event(event: str, **fields) -> None:
    """Append an event to the scheduler JSONL log."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    try:
        events_path = _sched_config().events_file
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with open(events_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _write_live_state(**fields) -> None:
    """Atomically write the live state JSON file."""
    try:
        live_path = _sched_config().live_state_file
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


def _portal_auth_headers() -> dict:
    """Headers carrying the portal auth token, if one is configured."""
    from .security import get_local_portal_token

    token = get_local_portal_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _notify_portal(task_name: str, status: str, duration: int, summary: str) -> None:
    """POST a scheduler_task_complete notification to the portal."""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        portal_url = get_config().portal.url
        timeout = _sched_config().portal_notify_timeout

        requests.post(
            f"{portal_url}/api/notify",
            json={
                "event": "scheduler_task_complete",
                "task": task_name,
                "status": status,
                "duration": duration,
                "summary": summary,
            },
            headers=_portal_auth_headers(),
            verify=False,
            timeout=timeout,
        )
    except Exception:
        pass  # Portal may not be running


def _notify_portal_state() -> None:
    """Push full scheduler live state to the portal via /api/notify."""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        state = read_live_state()
        if not state:
            return

        portal_url = get_config().portal.url
        timeout = _sched_config().portal_notify_timeout

        requests.post(
            f"{portal_url}/api/notify",
            json={"event": "scheduler_state", "running": True, **state},
            headers=_portal_auth_headers(),
            verify=False,
            timeout=timeout,
        )
    except Exception:
        pass  # Portal may not be running


def _parse_ensure_summary(task: SchedulerTask, result, project: str | None = None,
                          session: str | None = None) -> tuple[str, list[str], list[str]]:
    """Try to extract summary info from ensure subprocess output.

    `project`/`session` override `task.project`/`task.session` for worktree
    dispatch, where the summary file lives in the worktree and is named after
    the worktree session.

    Returns:
        (summary_text, files_modified, blockers)
    """
    project = project or task.project
    session = session or task.session
    summary = ""
    files_modified: list[str] = []
    blockers: list[str] = []

    # Try parsing JSON stdout from ensure --json. Stdout may contain several
    # concatenated JSON objects when nested commands (e.g. session creation)
    # also print JSON — the last object is ensure's own result.
    data = None
    if hasattr(result, "stdout") and result.stdout:
        decoder = json.JSONDecoder()
        text = result.stdout
        idx = 0
        while idx < len(text):
            try:
                obj, idx = decoder.raw_decode(text, idx)
            except ValueError:
                idx += 1
                continue
            if isinstance(obj, dict):
                data = obj
    if data:
        try:
            summary = data.get("summary", "")
            summary_file = data.get("summary_file", "")
            if summary_file:
                from .completion import parse_summary_file
                sp = Path(summary_file)
                if not sp.is_absolute():
                    sp = Path(project) / sp
                if sp.exists():
                    parsed = parse_summary_file(sp)
                    summary = summary or parsed.summary
                    files_modified = parsed.files_modified
                    blockers = parsed.blockers
        except Exception:
            pass
        # A failed dispatch has no summary file — record ensure's error so
        # the failure is self-describing in events/state instead of silent.
        if not summary and data.get("error"):
            summary = str(data["error"])

    # Fallback: glob for summary files if we didn't get one
    if not summary:
        try:
            agentwire_dir = Path(project) / ".agentwire"
            if agentwire_dir.exists():
                summaries = sorted(
                    agentwire_dir.glob(f"task-summary-{session}-{task.task}-*.md"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if summaries:
                    from .completion import parse_summary_file
                    parsed = parse_summary_file(summaries[0])
                    summary = parsed.summary
                    files_modified = parsed.files_modified
                    blockers = parsed.blockers
        except Exception:
            pass

    return summary, files_modified, blockers


def _collect_descendants(pid: int, result: list[int]) -> None:
    """Recursively collect all descendant PIDs."""
    try:
        children = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        if children.returncode == 0:
            for line in children.stdout.strip().split('\n'):
                if line.strip():
                    child_pid = int(line.strip())
                    result.append(child_pid)
                    _collect_descendants(child_pid, result)
    except Exception:
        pass


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its descendants. No-op if already dead."""
    try:
        descendants: list[int] = []
        _collect_descendants(pid, descendants)

        # Kill children first (bottom-up), then parent
        for child_pid in reversed(descendants):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass

        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    except Exception:
        pass


def _sweep_orphaned_processes() -> None:
    """Kill orphaned agent processes not inside any active tmux session.

    Safety net for processes that survive _kill_session().
    """
    # Get all descendant PIDs of active tmux panes
    active_pids: set[int] = set()
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    try:
                        pid = int(line.strip())
                        active_pids.add(pid)
                        # Collect all descendants of active panes
                        descendants: list[int] = []
                        _collect_descendants(pid, descendants)
                        active_pids.update(descendants)
                    except ValueError:
                        pass
    except Exception:
        pass



def _kill_session(session: str) -> None:
    """Kill a tmux session and all processes inside it."""
    check = subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"],
        capture_output=True,
    )
    if check.returncode != 0:
        return

    # Step 1: Get PIDs of processes running in all panes
    pids_result = subprocess.run(
        ["tmux", "list-panes", "-t", f"={session}", "-F", "#{pane_pid}"],
        capture_output=True, text=True, timeout=5,
    )
    pane_pids = []
    if pids_result.returncode == 0:
        for p in pids_result.stdout.strip().split('\n'):
            if p.strip():
                try:
                    pane_pids.append(int(p.strip()))
                except ValueError:
                    pass

    # Step 2: Send /exit to pane 0 for clean agent shutdown
    subprocess.run(
        ["tmux", "send-keys", "-t", f"={session}.0", "/exit", "Enter"],
        capture_output=True, timeout=5,
    )
    time.sleep(3)

    # Step 3: Kill tmux session
    subprocess.run(
        ["tmux", "kill-session", "-t", f"={session}"],
        capture_output=True, timeout=_sched_config().git_op_timeout,
    )

    # Step 4: Kill any surviving process trees from the panes
    for pid in pane_pids:
        _kill_process_tree(pid)

    print(f"[{_ts()}] Killed session: {session}")
    time.sleep(1)


def _pre_create_session(task: SchedulerTask) -> None:
    """Pre-create session with scheduler type/role overrides if needed.

    The scheduler may specify a different session type than the project's
    .agentwire.yml (e.g., claude-auto for scheduled tasks). If overrides
    are set, we need to create the session before ensure runs, because
    ensure uses project defaults.

    If no overrides, this is a no-op — ensure --fresh handles everything.
    """
    if not task.type and task.roles is None and not task.model:
        return  # No overrides, let ensure handle it

    # Only pre-create if session doesn't exist (ensure --fresh will have killed it)
    check = subprocess.run(
        ["tmux", "has-session", "-t", f"={task.session}"],
        capture_output=True,
    )
    if check.returncode == 0:
        return  # Already exists

    cmd = ["agentwire", "new", "-s", task.session, "-p", task.project]
    if task.type:
        cmd.extend(["--type", task.type])
    if task.roles is not None:
        cmd.extend(["--roles", ",".join(task.roles)])
    if task.model:
        cmd.extend(["--model", task.model])

    result = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=_sched_config().session_create_timeout,
                            env=_unattended_env(task))
    if result.returncode == 0:
        print(f"[{_ts()}] Pre-created session: {task.session} (type={task.type or 'default'}, model={task.model or 'default'})")
    else:
        print(f"[{_ts()}] Warning: Failed to pre-create session {task.session}: {result.stderr.strip()}")


def _task_is_persistent(task: SchedulerTask) -> bool:
    """Whether the project task opts out of session teardown (exit_on_complete: false).

    Persistent sessions double as interactive receivers — the scheduler must
    not kill them at dispatch. `ensure` reuses the live session instead.
    """
    from .tasks import load_task
    try:
        return not load_task(Path(task.project), task.task).exit_on_complete
    except Exception:
        return False


def _is_git_repo(project: str) -> bool:
    """True if `project` is inside a git work tree."""
    if not project:
        return False
    try:
        r = subprocess.run(
            ["git", "-C", project, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=_sched_config().git_timeout,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def _is_worktree_task(task: SchedulerTask) -> bool:
    """Whether a task runs in an isolated worktree and opens a PR.

    Explicit `worktree:` wins; otherwise it auto-enables when the task's
    project is a git repo (worktree+PR is the default for repo work; set
    `worktree: false` for research/email tasks that produce no repo files).
    """
    if task.worktree is not None:
        return bool(task.worktree)
    return _is_git_repo(task.project)


def _remove_scheduler_worktree(worktree_path: str, branch: str) -> None:
    """Remove a scheduler worktree and delete its local branch (best-effort)."""
    wt = Path(worktree_path)
    if wt.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            capture_output=True, text=True,
            timeout=_sched_config().git_op_timeout,
            cwd=str(wt) if wt.is_dir() else None,
        )
    # Best-effort branch cleanup from the canonical repo (parent of the
    # `<repo>-worktrees` dir, with the suffix dropped).
    if branch:
        worktrees_parent = wt.parent
        canonical = worktrees_parent.parent / worktrees_parent.name.removesuffix("-worktrees")
        if canonical.is_dir():
            subprocess.run(
                ["git", "branch", "-D", branch],
                capture_output=True, text=True, timeout=_sched_config().git_timeout,
                cwd=str(canonical), check=False,
            )


def _pr_number_from_url(url: str) -> int | None:
    """Parse the trailing PR number from a `gh pr create` URL."""
    m = re.search(r"/pull/(\d+)", url.strip())
    return int(m.group(1)) if m else None


def _finalize_worktree_pr(task: SchedulerTask, task_name: str, status: str,
                          worktree_path: str, branch: str, summary: str) -> dict:
    """Commit + push + open a draft PR from an isolated worktree.

    `git add -A` is safe here — the worktree was forked fresh off the base
    branch, so there's no unrelated work to sweep. Returns
    ``{"number": int|None, "url": str}`` when a PR is opened, or ``{}`` when
    there was nothing to commit (in which case the empty worktree is removed).
    """
    cfg = _sched_config()
    import shutil

    st = subprocess.run(
        ["git", "-C", worktree_path, "status", "--porcelain"],
        capture_output=True, text=True, timeout=cfg.git_timeout,
    )
    if st.returncode != 0:
        return {}
    if not st.stdout.strip():
        # Task produced no changes — don't leave an empty worktree lying around.
        _remove_scheduler_worktree(worktree_path, branch)
        print(f"[{_ts()}] {task_name}: no changes — worktree removed, no PR")
        return {}

    msg = f"scheduler: {task_name} — {(summary or status).strip()[:60]}"
    subprocess.run(["git", "-C", worktree_path, "add", "-A"],
                   capture_output=True, timeout=cfg.git_timeout)
    commit = subprocess.run(
        ["git", "-C", worktree_path, "commit", "-m", msg, "--no-verify"],
        capture_output=True, text=True, timeout=cfg.git_op_timeout,
    )
    if commit.returncode != 0:
        print(f"[{_ts()}] {task_name}: commit failed: {commit.stderr.strip()}")
        return {}

    push = subprocess.run(
        ["git", "-C", worktree_path, "push", "-u", "origin", branch],
        capture_output=True, text=True, timeout=cfg.git_op_timeout,
    )
    if push.returncode != 0:
        # Keep the worktree so the work isn't lost; it'll be swept as an orphan.
        print(f"[{_ts()}] {task_name}: push failed: {push.stderr.strip()}")
        return {}

    if shutil.which("gh") is None:
        print(f"[{_ts()}] {task_name}: 'gh' not found — branch pushed, no PR")
        return {}

    pr_target = task.pr_target or task.base
    title = f"scheduler: {task_name} — {(summary or status).strip()[:50]}"
    body = (summary.strip() if summary else
            f"Automated changes from scheduled task `{task_name}` (status: {status}).")
    pr_cmd = ["gh", "pr", "create", "--base", pr_target, "--head", branch,
              "--title", title, "--body", body]
    if task.pr_draft:
        pr_cmd.append("--draft")
    pr = subprocess.run(pr_cmd, cwd=worktree_path, capture_output=True,
                        text=True, timeout=cfg.git_op_timeout)
    if pr.returncode != 0:
        print(f"[{_ts()}] {task_name}: PR creation failed: {pr.stderr.strip()}")
        return {}

    url = pr.stdout.strip()
    number = _pr_number_from_url(url)
    print(f"[{_ts()}] {task_name}: opened PR {url}")
    return {"number": number, "url": url}


def _pr_state(pr_number: int, cwd: str | None) -> str | None:
    """Return a PR's state (``OPEN``/``MERGED``/``CLOSED``) via gh, or None on error."""
    import shutil
    if shutil.which("gh") is None:
        return None
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state", "-q", ".state"],
            capture_output=True, text=True,
            timeout=_sched_config().git_op_timeout, cwd=cwd,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except Exception:
        pass
    return None


def reap_worktree_prs(board: Board) -> list[dict]:
    """Tear down worktrees whose PR is no longer OPEN (merged or closed).

    Kills the worker session if still alive, removes the worktree + branch,
    and clears the tracking fields so the task can run fresh next time.
    Self-limiting: only touches tasks that have an open tracked PR.
    """
    reaped: list[dict] = []
    for name, st in list(board.state.items()):
        if not st.pr_number or not st.worktree_path:
            continue

        cwd = st.worktree_path if Path(st.worktree_path).exists() else None
        if cwd is None:
            task = board.tasks.get(name)
            cwd = task.project if task and Path(task.project).exists() else None

        state = _pr_state(st.pr_number, cwd)
        if state is None or state == "OPEN":
            continue  # error/unknown, or still under review — leave it be

        if st.worktree_session:
            _kill_session(st.worktree_session)
        _remove_scheduler_worktree(st.worktree_path, st.worktree_branch)

        record = {"task": name, "pr": st.pr_number, "pr_state": state,
                  "worktree": st.worktree_path}
        st.worktree_branch = ""
        st.worktree_path = ""
        st.worktree_session = ""
        st.pr_number = None
        st.pr_url = ""
        save_board(board)
        _log_event("worktree_reaped", task=name, pr=record["pr"], pr_state=state)
        print(f"[{_ts()}] Reaped worktree for {name} (PR #{record['pr']} {state})")
        reaped.append(record)
    return reaped


def dispatch_task(board: Board, task_name: str) -> TaskState:
    """Run a task via `agentwire ensure` and return updated state.

    Args:
        board: Current board (for reading task config).
        task_name: Name of the task to dispatch.

    Returns:
        Updated TaskState with results.
    """
    task = board.tasks[task_name]
    existing_state = board.state.get(task_name, TaskState())

    _gated_tasks.discard(task_name)

    existing_state.last_dispatch = datetime.now(timezone.utc)
    board.state[task_name] = existing_state
    save_board(board)

    _log_event("task_started", task=task_name, session=task.session,
               project=task.project, attempt=existing_state.run_count + 1)

    return _dispatch_ensure_task(board, task, existing_state)


def _dispatch_ensure_task(board: Board, task: SchedulerTask, existing_state: TaskState) -> TaskState:
    """Dispatch via `agentwire ensure` subprocess (tmux session path)."""
    if _is_worktree_task(task):
        return _dispatch_worktree_task(board, task, existing_state)
    return _dispatch_inplace_task(board, task, existing_state)


def _unattended_env(task: SchedulerTask) -> dict[str, str]:
    """Env overlay marking a headless dispatch as unattended (no human present).

    THE single chokepoint where the scheduler declares a run unattended. Every
    dispatch gets ``AGENTWIRE_UNATTENDED=1``; if the project task defines
    ``unattended_allow`` (its per-task extension to the global allowlist), those
    rule ids are passed via ``AGENTWIRE_UNATTENDED_ALLOW``. This overlay is the
    subprocess ``env=`` for the ensure/new dispatch calls; ``agentwire``'s
    ``_build_tmux_env_flags`` funnels both vars into the new tmux session before
    the agent launches, where the damage-control hook reads them. Interactive
    sessions never go through here, so the marker can't leak into them.
    """
    env = dict(os.environ)
    env["AGENTWIRE_UNATTENDED"] = "1"
    try:
        from .tasks import load_task
        tc = load_task(Path(task.project), task.task)
        extra = getattr(tc, "unattended_allow", None)
        if extra:
            env["AGENTWIRE_UNATTENDED_ALLOW"] = ",".join(str(x) for x in extra if x)
    except Exception:
        pass
    return env


def _run_ensure(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, "subprocess.CompletedProcess | None", int]:
    """Run an `agentwire ensure` subprocess; return (exit_code, result, duration_s)."""
    start_time = time.time()
    result = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, env=env,
        )
        stdout, stderr = proc.communicate()
        exit_code = proc.returncode
        result = subprocess.CompletedProcess(cmd, exit_code, stdout, stderr)
    except Exception:
        exit_code = _EXIT_FAILED
    return exit_code, result, int(time.time() - start_time)


def _apply_max_runs(board: Board, task: SchedulerTask, new_state: TaskState,
                    new_run_count: int) -> None:
    """Auto-disable a task that has hit its max_runs cap."""
    if task.max_runs is not None and new_run_count >= task.max_runs:
        task.enabled = False
        board.state[task.name] = new_state
        save_board(board)
        _log_event("task_disabled", task=task.name, reason="max_runs_reached",
                   run_count=new_run_count, max_runs=task.max_runs)


def _dispatch_inplace_task(board: Board, task: SchedulerTask, existing_state: TaskState) -> TaskState:
    """Run a non-worktree task in place. Produces no commits — side effects
    only (email, files outside the repo). The repo is never modified."""
    task_name = task.name

    # A session parked on a usage limit is waiting out the reset — never
    # kill it or dispatch into it. Skip without consuming last_run so the
    # task stays eligible once the park clears.
    from .usage_limit import is_parked
    if is_parked(task.session):
        _log_event("task_skipped", task=task_name, session=task.session,
                   reason="usage_limit_parked")
        return TaskState(
            last_run=existing_state.last_run,
            last_status="usage_limit",
            last_duration=0,
            run_count=existing_state.run_count,
        )

    # Clean stale lock for this session before dispatching.
    from .locking import remove_stale_lock
    remove_stale_lock(task.session)

    # Tasks with exit_on_complete: false keep a long-lived session that may be
    # in interactive use — never kill it at dispatch; ensure reuses it as-is.
    persistent = _task_is_persistent(task)

    has_overrides = bool(task.type or task.roles is not None or task.model)
    if has_overrides:
        # Scheduler has type/role overrides — kill + pre-create ourselves,
        # then let ensure reuse the session (no --fresh)
        if not persistent:
            _kill_session(task.session)
        _pre_create_session(task)  # no-op if the session already exists

    cmd = [
        "agentwire", "ensure",
        "-s", task.session,
        "--task", task.task,
        "--project", task.project,
        "--json",
    ]
    if not has_overrides and not persistent:
        # No type/role overrides — kill stale session so ensure creates fresh
        _kill_session(task.session)

    exit_code, result, duration = _run_ensure(cmd, env=_unattended_env(task))
    status = _EXIT_TO_STATUS.get(exit_code, "failed")

    # On lock conflict, don't update last_run so task remains eligible
    if exit_code == _EXIT_LOCK_CONFLICT:
        _log_event("task_skipped", task=task_name, session=task.session,
                    reason="lock_conflict")
        return TaskState(
            last_run=existing_state.last_run,
            last_status="lock_conflict",
            last_duration=duration,
            run_count=existing_state.run_count,
        )

    summary, files_modified, blockers_list = _parse_ensure_summary(task, result)

    _log_event("task_completed", task=task_name, session=task.session,
               status=status, duration=duration, summary=summary,
               files_modified=files_modified, blockers=blockers_list)
    _notify_portal(task_name, status, duration, summary)

    new_run_count = existing_state.run_count + 1
    new_state = TaskState(
        last_run=datetime.now(timezone.utc),
        last_status=status,
        last_duration=duration,
        run_count=new_run_count,
        last_summary=summary,
        last_gate_commit=_capture_head(task.project),
    )
    _apply_max_runs(board, task, new_state, new_run_count)
    return new_state


def _dispatch_worktree_task(board: Board, task: SchedulerTask, existing_state: TaskState) -> TaskState:
    """Run a task in a fresh git worktree forked off `base`, then commit +
    push + open a draft PR. The live project folder is never touched, and
    `git add -A` in the worktree is safe (no unrelated work to sweep)."""
    task_name = task.name
    from .locking import remove_stale_lock
    cfg = _sched_config()

    proj = Path(task.project)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"scheduler-{task_name}-{ts}"
    wt_session = f"{proj.name}/{branch}"  # slash → ensure/new treat as worktree

    remove_stale_lock(wt_session)
    _kill_session(wt_session)  # paranoia — a timestamped session shouldn't exist

    # Create the worktree + session off the latest origin/<base>.
    # --kind orchestrator: a project/branch name would otherwise derive the
    # 'worktree-session' kind, whose etiquette tells the agent to open its OWN
    # draft PR + notify. But the scheduler is the deterministic finalizer
    # (_finalize_worktree_pr opens + tracks the PR; reap_worktree_prs only
    # reaps TRACKED PRs) — an agent-opened PR would escape the reaper and leak.
    # The replaceable orchestrator kind lets the task's own roles win, so no
    # worktree-session PR-opening contract is injected.
    new_cmd = [
        "agentwire", "new", "-s", wt_session, "-p", task.project,
        "--base", task.base, "--pull-first", "--kind", "orchestrator", "--json",
    ]
    if task.type:
        new_cmd += ["--type", task.type]
    if task.roles is not None:
        new_cmd += ["--roles", ",".join(task.roles)]
    if task.model:
        new_cmd += ["--model", task.model]

    unattended_env = _unattended_env(task)
    worktree_path = ""
    new_res = None
    try:
        new_res = subprocess.run(new_cmd, capture_output=True, text=True,
                                 timeout=cfg.session_create_timeout,
                                 env=unattended_env)
        if new_res.returncode == 0:
            worktree_path = (json.loads(new_res.stdout) or {}).get("path", "")
    except Exception:
        worktree_path = ""

    if not worktree_path:
        err = (new_res.stderr or new_res.stdout).strip()[:200] if new_res else ""
        _log_event("task_failed", task=task_name, session=wt_session,
                   reason=f"worktree create failed: {err}")
        print(f"[{_ts()}] {task_name}: worktree create failed")
        new_run_count = existing_state.run_count + 1
        return TaskState(
            last_run=datetime.now(timezone.utc), last_status="failed",
            last_duration=0, run_count=new_run_count,
            last_summary="worktree creation failed",
        )

    # Run the task inside the worktree.
    cmd = [
        "agentwire", "ensure", "-s", wt_session, "--task", task.task,
        "--project", worktree_path, "--json",
    ]
    exit_code, result, duration = _run_ensure(cmd, env=unattended_env)
    status = _EXIT_TO_STATUS.get(exit_code, "failed")

    if exit_code == _EXIT_LOCK_CONFLICT:
        _log_event("task_skipped", task=task_name, session=wt_session,
                    reason="lock_conflict")
        _remove_scheduler_worktree(worktree_path, branch)  # task didn't run
        return TaskState(
            last_run=existing_state.last_run, last_status="lock_conflict",
            last_duration=duration, run_count=existing_state.run_count,
        )

    summary, files_modified, blockers_list = _parse_ensure_summary(
        task, result, project=worktree_path, session=wt_session)

    _log_event("task_completed", task=task_name, session=wt_session,
               status=status, duration=duration, summary=summary,
               files_modified=files_modified, blockers=blockers_list)
    _notify_portal(task_name, status, duration, summary)

    if status == "usage_limit":
        # Session parked mid-task — committing/pushing half-done work would be
        # wrong, and the session must stay alive for the watchdog to resume.
        # The worktree is left in place; the session finishes via the idle
        # hook after resume (PR must then be opened manually — see docs).
        print(f"[{_ts()}] {task_name}: parked on usage limit — worktree + session left in place")
        new_run_count = existing_state.run_count + 1
        return TaskState(
            last_run=datetime.now(timezone.utc),
            last_status="usage_limit",
            last_duration=duration,
            run_count=new_run_count,
            last_summary=summary,
            last_gate_commit=_capture_head(task.project),
        )

    pr = _finalize_worktree_pr(task, task_name, status, worktree_path, branch, summary)

    new_run_count = existing_state.run_count + 1
    new_state = TaskState(
        last_run=datetime.now(timezone.utc),
        last_status=status,
        last_duration=duration,
        run_count=new_run_count,
        last_summary=summary,
        last_gate_commit=_capture_head(task.project),
        worktree_branch=branch if pr else "",
        worktree_path=worktree_path if pr else "",
        worktree_session=wt_session if pr else "",
        pr_number=pr.get("number") if pr else None,
        pr_url=pr.get("url", "") if pr else "",
    )
    _apply_max_runs(board, task, new_state, new_run_count)
    return new_state


def _capture_head(project: str) -> str:
    """Return the current HEAD SHA of a git repo, or '' if unavailable."""
    if not project:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", project, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=_sched_config().git_timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


# Workflow dispatch ---------------------------------------------------------

_WORKFLOW_STATUS_TO_SCHED = {
    "success": "complete",
    "partial": "incomplete",
    "failure": "failed",
}


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
        rows.append(row)

    # Sort: enabled first, then by overdue (most overdue first)
    rows.sort(key=lambda r: (not r["enabled"], -r["overdue_by"]))
    return rows


def run_scheduler_loop() -> None:
    """Main scheduler daemon loop. Runs forever."""
    started_at = datetime.now(timezone.utc)
    tasks_completed = 0
    tasks_failed = 0
    loop_count = 0

    print(f"[{_ts()}] Scheduler starting...")
    print(f"[{_ts()}] Board: {_sched_config().board_file}")

    try:
        board = load_board()
    except (FileNotFoundError, ValueError) as e:
        print(f"[{_ts()}] Error: {e}", file=sys.stderr)
        sys.exit(1)

    task_count = len(board.tasks)
    enabled_count = sum(1 for t in board.tasks.values() if t.enabled)
    print(f"[{_ts()}] Loaded {task_count} tasks ({enabled_count} enabled)")

    _log_event("scheduler_started", task_count=task_count, enabled_count=enabled_count)
    _write_live_state(
        status="running",
        started_at=started_at.isoformat(),
        current_task=None,
        current_task_started=None,
        tasks_completed=0,
        tasks_failed=0,
        uptime_seconds=0,
        next_task=None,
        next_in_seconds=0,
    )
    _notify_portal_state()

    while True:
        max_sleep = _sched_config().max_loop_sleep

        try:
            board = load_board()
        except (FileNotFoundError, ValueError) as e:
            print(f"[{_ts()}] Board read error: {e}", file=sys.stderr)
            time.sleep(max_sleep)
            continue

        # Reap worktrees whose PR has merged/closed (cheap — only tasks with
        # an open tracked PR hit `gh`).
        try:
            reap_worktree_prs(board)
        except Exception as e:
            print(f"[{_ts()}] Worktree reap error: {e}", file=sys.stderr)

        task_name, wait_seconds = pick_next_task(board)
        uptime = int((datetime.now(timezone.utc) - started_at).total_seconds())

        if task_name is None:
            sleep_time = min(wait_seconds, max_sleep)
            print(f"[{_ts()}] Nothing due. Sleeping {int(sleep_time)}s...")
            _write_live_state(
                status="running",
                started_at=started_at.isoformat(),
                current_task=None,
                current_task_started=None,
                tasks_completed=tasks_completed,
                tasks_failed=tasks_failed,
                uptime_seconds=uptime,
                next_task=None,
                next_in_seconds=round(sleep_time, 1),
            )
            _notify_portal_state()
            time.sleep(sleep_time)
            continue

        if wait_seconds > 0:
            sleep_time = min(wait_seconds, max_sleep)
            print(f"[{_ts()}] Next: {task_name} in {format_interval(int(wait_seconds))}. Sleeping {int(sleep_time)}s...")
            _log_event("scheduler_sleeping", next_task=task_name,
                        sleep_seconds=round(sleep_time, 1))
            _write_live_state(
                status="running",
                started_at=started_at.isoformat(),
                current_task=None,
                current_task_started=None,
                tasks_completed=tasks_completed,
                tasks_failed=tasks_failed,
                uptime_seconds=uptime,
                next_task=task_name,
                next_in_seconds=round(wait_seconds, 1),
            )
            _notify_portal_state()
            time.sleep(sleep_time)
            continue

        # Dispatch the task
        print(f"[{_ts()}] Running: {task_name}")
        task_started = datetime.now(timezone.utc)
        _write_live_state(
            status="running",
            started_at=started_at.isoformat(),
            current_task=task_name,
            current_task_started=task_started.isoformat(),
            tasks_completed=tasks_completed,
            tasks_failed=tasks_failed,
            uptime_seconds=uptime,
            next_task=None,
            next_in_seconds=0,
        )
        _notify_portal_state()

        state = dispatch_task(board, task_name)
        board.state[task_name] = state
        save_board(board)

        if state.last_status == "complete":
            tasks_completed += 1
        elif state.last_status not in ("lock_conflict", "never"):
            tasks_failed += 1

        print(f"[{_ts()}] Done: {task_name} → {state.last_status} ({state.last_duration}s)")

        cooldown = _sched_config().dispatch_cooldown
        if cooldown > 0:
            print(f"[{_ts()}] Cooldown: {cooldown}s")
            time.sleep(cooldown)

        # Periodic orphan sweep (every ~10 iterations)
        loop_count += 1
        if loop_count % 10 == 0:
            _sweep_orphaned_processes()


def read_events(tail: int = 20, task_filter: str | None = None) -> list[dict]:
    """Read recent events from the JSONL log.

    Args:
        tail: Number of most recent events to return.
        task_filter: Only return events for this task name.

    Returns:
        List of event dicts, most recent last.
    """
    events_path = _sched_config().events_file
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
    live_path = _sched_config().live_state_file
    if not live_path.exists():
        return None
    try:
        return json.loads(live_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _validate_task_payload(name: str, task: SchedulerTask) -> list[str]:
    """Validate the ensure-task payload shape for a single scheduler entry."""
    errors: list[str] = []

    if not task.task:
        errors.append(f"{name}: must set 'task'")

    gate = task.gate or {}
    if isinstance(gate, dict):
        needs_project = bool(gate.get("git_commit") or gate.get("git_diff"))
        if needs_project and not task.project:
            gate_type = "git_commit" if gate.get("git_commit") else "git_diff"
            errors.append(f"{name}: gate {gate_type} requires 'project' path")

    return errors


def validate_board(board: Board) -> list[str]:
    """Validate board configuration for errors.

    Returns list of error/warning strings (empty = valid).
    """
    errors = []

    for name, task in board.tasks.items():
        sched = task.schedule
        if not sched.every and not sched.after:
            errors.append(f"{name}: schedule must have 'every' or 'after' (or both)")

        if sched.every and sched.every in _CALENDAR_EVERY and sched.at:
            t = _parse_time(sched.at)
            if not t:
                errors.append(f"{name}: invalid 'at' time '{sched.at}' (expected HH:MM)")

        if sched.every and sched.every not in _CALENDAR_EVERY:
            d = _parse_duration(sched.every)
            if d is None:
                errors.append(f"{name}: invalid 'every' value '{sched.every}'")

        if sched.after and sched.after not in board.tasks:
            errors.append(f"{name}: dependency '{sched.after}' not found in board")

        if sched.after and sched.after in board.tasks and not board.tasks[sched.after].enabled:
            errors.append(f"{name}: warning: dependency '{sched.after}' is disabled")

        errors.extend(_validate_task_payload(name, task))

    # Circular dependency detection via DFS
    def _has_cycle(start: str, visited: set, path: set) -> bool:
        if start in path:
            return True
        if start in visited:
            return False
        visited.add(start)
        path.add(start)
        task = board.tasks.get(start)
        if task and task.schedule.after:
            if _has_cycle(task.schedule.after, visited, path):
                return True
        path.discard(start)
        return False

    visited: set[str] = set()
    for name in board.tasks:
        if _has_cycle(name, visited, set()):
            errors.append(f"{name}: circular dependency detected")

    return errors


def _ts() -> str:
    """Current timestamp for log lines."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
