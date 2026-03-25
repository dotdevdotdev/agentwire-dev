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

_EXIT_TO_STATUS = {
    _EXIT_COMPLETE: "complete",
    _EXIT_FAILED: "failed",
    _EXIT_INCOMPLETE: "incomplete",
    _EXIT_LOCK_CONFLICT: "lock_conflict",
    _EXIT_PRE_FAILURE: "failed",
    _EXIT_TIMEOUT: "timeout",
    _EXIT_SESSION_ERROR: "failed",
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
    project: str          # ~/projects/foo (expanded at load time)
    session: str          # session name for ensure
    task: str             # task name in project's .agentwire.yml
    schedule: Schedule    # REQUIRED (replaces interval)
    enabled: bool = True
    filler: bool = False  # only runs in spare cycles
    priority: int = 99    # task ordering (lower = higher priority)
    type: str | None = None  # session type override (e.g., claude-bypass)
    roles: list[str] | None = None  # role override (e.g., ["task-runner"])
    model: str | None = None  # model override (e.g., "haiku")
    gate: dict | None = None  # precondition gate (git_commit, git_diff, command)
    max_runs: int | None = None  # auto-disable after N successful dispatches
    once: bool = False           # shorthand for max_runs: 1


@dataclass
class TaskState:
    last_run: datetime | None = None
    last_status: str = "never"    # complete, failed, incomplete, timeout, lock_conflict, never
    last_duration: int = 0
    run_count: int = 0
    last_summary: str = ""
    last_gate_commit: str = ""    # HEAD at last dispatch (for gate checks)
    last_dispatch: datetime | None = None  # set BEFORE running (restart safety)


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

        board.tasks[name] = SchedulerTask(
            name=name,
            project=str(Path(t.get("project", "")).expanduser()),
            session=t.get("session", name),
            task=t.get("task", name),
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
                last_gate_commit=str(s.get("last_gate_commit", "")),
                last_dispatch=_parse_datetime_field(s.get("last_dispatch")),
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
        if s.last_gate_commit:
            entry["last_gate_commit"] = s.last_gate_commit
        if s.last_dispatch:
            entry["last_dispatch"] = s.last_dispatch.isoformat()
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


def _check_gate(board: Board, task_name: str) -> bool:
    """Return True if task should run, False to skip.

    Evaluates gate preconditions defined on the task. Multiple gate keys
    are AND'd — all must pass. Fails open (returns True) on errors,
    missing baseline, or no gate defined.

    Only logs the first time a task is gated — subsequent checks for the
    same task are silent until the task runs or conditions change.
    """
    task = board.tasks[task_name]
    gate = task.gate
    if not gate or not isinstance(gate, dict):
        _gated_tasks.discard(task_name)
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
        except Exception:
            _gated_tasks.discard(task_name)
            return True  # Fail open

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
        except Exception:
            _gated_tasks.discard(task_name)
            return True  # Fail open

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
        except Exception:
            _gated_tasks.discard(task_name)
            return True  # Fail open

    # Gate passed — clear from gated set so it can be re-reported if gated again later
    _gated_tasks.discard(task_name)
    return True


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
            json={"event": "scheduler_state", **state},
            verify=False,
            timeout=timeout,
        )
    except Exception:
        pass  # Portal may not be running


def _parse_ensure_summary(task: SchedulerTask, result) -> tuple[str, list[str], list[str]]:
    """Try to extract summary info from ensure subprocess output.

    Returns:
        (summary_text, files_modified, blockers)
    """
    summary = ""
    files_modified: list[str] = []
    blockers: list[str] = []

    # Try parsing JSON stdout from ensure --json
    if hasattr(result, "stdout") and result.stdout:
        try:
            data = json.loads(result.stdout)
            summary = data.get("summary", "")
            summary_file = data.get("summary_file", "")
            if summary_file:
                from .completion import parse_summary_file
                sp = Path(summary_file)
                if not sp.is_absolute():
                    sp = Path(task.project) / sp
                if sp.exists():
                    parsed = parse_summary_file(sp)
                    summary = summary or parsed.summary
                    files_modified = parsed.files_modified
                    blockers = parsed.blockers
        except (json.JSONDecodeError, Exception):
            pass

    # Fallback: glob for summary files if we didn't get one
    if not summary:
        try:
            agentwire_dir = Path(task.project) / ".agentwire"
            if agentwire_dir.exists():
                summaries = sorted(
                    agentwire_dir.glob(f"task-summary-{task.session}-{task.task}-*.md"),
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
        ["tmux", "send-keys", "-t", f"={session}:0.0", "/exit", "Enter"],
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
    .agentwire.yml (e.g., claudeglm-bypass for scheduled tasks). If overrides
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

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=_sched_config().session_create_timeout)
    if result.returncode == 0:
        print(f"[{_ts()}] Pre-created session: {task.session} (type={task.type or 'default'}, model={task.model or 'default'})")
    else:
        print(f"[{_ts()}] Warning: Failed to pre-create session {task.session}: {result.stderr.strip()}")


def _auto_commit(task: SchedulerTask, task_name: str, status: str) -> None:
    """Auto-commit any changes the task made in the project directory.

    Creates a standardized commit message so each task run is a single
    revertable commit. No-op if there are no changes to commit.
    """
    cfg = _sched_config()
    project = task.project

    # Check if there are any changes to commit
    check = subprocess.run(
        ["git", "-C", project, "status", "--porcelain"],
        capture_output=True, text=True, timeout=cfg.git_timeout,
    )
    if check.returncode != 0 or not check.stdout.strip():
        return  # Not a git repo or no changes

    # Stage all changes, but protect project config from agent modifications
    subprocess.run(
        ["git", "-C", project, "add", "-A"],
        capture_output=True, timeout=cfg.git_timeout,
    )
    subprocess.run(
        ["git", "-C", project, "reset", "HEAD", "--", ".agentwire.yml"],
        capture_output=True, timeout=cfg.git_timeout,
    )
    subprocess.run(
        ["git", "-C", project, "checkout", "--", ".agentwire.yml"],
        capture_output=True, timeout=cfg.git_timeout,
    )

    # Re-check if anything is still staged after excluding protected files
    check = subprocess.run(
        ["git", "-C", project, "diff", "--cached", "--quiet"],
        capture_output=True, timeout=cfg.git_timeout,
    )
    if check.returncode == 0:
        return  # Nothing left to commit

    # Commit with standardized message
    msg = f"scheduler: {task_name} ({status})"
    subprocess.run(
        ["git", "-C", project, "commit", "-m", msg, "--no-verify"],
        capture_output=True, text=True, timeout=cfg.git_op_timeout,
    )
    print(f"[{_ts()}] Auto-committed: {msg}")


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

    # Clear from gated set so gate can be re-evaluated after this run
    _gated_tasks.discard(task_name)

    # Clean stale lock for this session before dispatching.
    # Stale locks (from crashed ensure processes) cause --skip-if-locked
    # to silently exit 0, making tasks appear to complete instantly.
    from .locking import remove_stale_lock
    remove_stale_lock(task.session)

    # Set last_dispatch BEFORE running for restart safety
    existing_state.last_dispatch = datetime.now(timezone.utc)
    board.state[task_name] = existing_state
    save_board(board)

    _log_event("task_started", task=task_name, session=task.session,
               project=task.project, attempt=existing_state.run_count + 1)

    has_overrides = bool(task.type or task.roles is not None or task.model)

    if has_overrides:
        # Scheduler has type/role overrides — kill + pre-create ourselves,
        # then let ensure reuse the session (no --fresh)
        _kill_session(task.session)
        _pre_create_session(task)

    cmd = [
        "agentwire", "ensure",
        "-s", task.session,
        "--task", task.task,
        "--project", task.project,
        "--json",
    ]
    if not has_overrides:
        # No type/role overrides — kill stale session so ensure creates fresh
        _kill_session(task.session)

    start_time = time.time()
    result = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # Create new process group
        )
        stdout, stderr = proc.communicate()
        exit_code = proc.returncode
        result = subprocess.CompletedProcess(cmd, exit_code, stdout, stderr)
    except Exception:
        exit_code = _EXIT_FAILED

    duration = int(time.time() - start_time)
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

    # Parse summary from ensure output
    summary, files_modified, blockers_list = _parse_ensure_summary(task, result)

    # Log completion event
    _log_event("task_completed", task=task_name, session=task.session,
               status=status, duration=duration, summary=summary,
               files_modified=files_modified, blockers=blockers_list)

    # Notify portal
    _notify_portal(task_name, status, duration, summary)

    # Auto-commit any changes the task made (each task = one revertable commit)
    _auto_commit(task, task_name, status)

    # Capture HEAD for gate checks on next run
    gate_commit = ""
    try:
        head_result = subprocess.run(
            ["git", "-C", task.project, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=_sched_config().git_timeout,
        )
        if head_result.returncode == 0:
            gate_commit = head_result.stdout.strip()
    except Exception:
        pass

    new_run_count = existing_state.run_count + 1
    new_state = TaskState(
        last_run=datetime.now(timezone.utc),
        last_status=status,
        last_duration=duration,
        run_count=new_run_count,
        last_summary=summary,
        last_gate_commit=gate_commit,
    )

    # Auto-disable if max_runs reached
    if task.max_runs is not None and new_run_count >= task.max_runs:
        task.enabled = False
        board.state[task_name] = new_state
        save_board(board)
        _log_event("task_disabled", task=task_name, reason="max_runs_reached",
                   run_count=new_run_count, max_runs=task.max_runs)

    return new_state


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
