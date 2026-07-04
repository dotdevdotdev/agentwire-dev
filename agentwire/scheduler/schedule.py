"""Schedule math — eligibility, recurrence, task picking, and board validation."""

import time
from datetime import datetime, timezone

from .models import (
    _CALENDAR_EVERY,
    _DAY_NAMES,
    _IN_FLIGHT_GRACE,
    Board,
    Schedule,
    TaskState,
    _parse_duration,
    _parse_time,
)

# Minimum sleep when the board has a DUE task that isn't runnable (gate
# failed). Without it, pick_next_task returns (None, 0.0) and the main loop
# spins at time.sleep(0) — see #691.
GATE_RETRY_FLOOR = 30.0


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
    from agentwire import scheduler as _sched

    now_local = _sched.datetime.now()
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
    from agentwire import scheduler as _sched

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
    now_local = _sched.datetime.now()

    # Find today's target time
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
    from agentwire import scheduler as _sched

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
        if not _day_matches(_sched.datetime.now(), task.schedule.every, task.schedule.except_days):
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
        if _sched._check_gate(board, name):
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
        if not _day_matches(_sched.datetime.now(), task.schedule.every, task.schedule.except_days):
            continue
        eligible_ts = _compute_next_eligible(board, name)
        if eligible_ts is None:
            continue
        overdue_by = now - eligible_ts
        if overdue_by >= 0:
            filler_candidates.append((name, overdue_by))
    filler_candidates.sort(key=lambda x: (board.tasks[x[0]].priority, -x[1]))

    for name, _score in filler_candidates:
        if _sched._check_gate(board, name):
            return name, 0.0

    # Nothing to run now — calculate sleep until earliest task is due. A task
    # can be DUE yet not runnable (gate failed, above), which makes
    # seconds_until_next_due return 0.0 — without a floor the main loop does
    # time.sleep(0) and spins hard: gate command re-run + portal notify
    # ~12×/sec until the gate ever passes (#691). Floor to GATE_RETRY_FLOOR so
    # a due-but-gated board re-checks at loop cadence instead.
    wait = seconds_until_next_due(board)
    return None, max(wait, GATE_RETRY_FLOOR)


def seconds_until_next_due(board: Board) -> float:
    """Calculate seconds until the earliest task is due.

    Returns:
        Seconds to wait (0 if something is already due, 60.0 max as fallback).
    """
    from agentwire import scheduler as _sched

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
        return float(_sched._sched_config().max_loop_sleep)

    return earliest_wait


def _validate_task_payload(name: str, task) -> list[str]:
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
