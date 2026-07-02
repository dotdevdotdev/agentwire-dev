"""Main scheduler daemon loop and crash-loop-guarded board loading."""

import sys
import time
from datetime import datetime, timezone

from .models import Board, _ts


def _board_load_backoff(attempt: int) -> float:
    """Exponential backoff (seconds) for the Nth consecutive board-load failure.

    base * 2**attempt, capped at error_backoff_max. This is what stops a bad
    board from tight-looping: instead of respawning every ~30s and emitting
    millions of log lines, the daemon stays alive and backs off (#449).
    """
    from agentwire import scheduler as _sched

    cfg = _sched._sched_config()
    base = getattr(cfg, "error_backoff_base", 30)
    cap = getattr(cfg, "error_backoff_max", 1800)
    return float(min(cap, base * (2 ** min(max(attempt, 0), 16))))


def _load_board_blocking(started_at: datetime) -> Board:
    """Load the board, retrying with backoff instead of exiting on failure.

    Staying alive (rather than sys.exit) is the whole point: an external
    KeepAlive supervisor never sees the process die, so it can't respawn-storm.
    Identical errors are logged once (not every attempt) to bound log growth.
    """
    from agentwire import scheduler as _sched

    attempt = 0
    last_err: str | None = None
    while True:
        try:
            return _sched.load_board()
        except (FileNotFoundError, ValueError) as e:
            wait = _board_load_backoff(attempt)
            msg = str(e)
            if msg != last_err:
                print(f"[{_ts()}] Board load failed: {msg} — backing off "
                      f"{int(wait)}s (attempt {attempt + 1})", file=sys.stderr)
                _sched._log_event("board_load_error", error=msg[:200], backoff=wait,
                                  attempt=attempt + 1)
                last_err = msg
            uptime = int((datetime.now(timezone.utc) - started_at).total_seconds())
            _sched._write_live_state(
                status="error",
                started_at=started_at.isoformat(),
                error=msg[:200],
                uptime_seconds=uptime,
            )
            attempt += 1
            time.sleep(wait)


def run_scheduler_loop() -> None:
    """Main scheduler daemon loop. Runs forever."""
    from agentwire import scheduler as _sched

    started_at = datetime.now(timezone.utc)
    tasks_completed = 0
    tasks_failed = 0
    loop_count = 0

    print(f"[{_ts()}] Scheduler starting...")
    print(f"[{_ts()}] Board: {_sched._sched_config().board_file}")

    board = _load_board_blocking(started_at)

    task_count = len(board.tasks)
    enabled_count = sum(1 for t in board.tasks.values() if t.enabled)
    print(f"[{_ts()}] Loaded {task_count} tasks ({enabled_count} enabled)")

    _sched._log_event("scheduler_started", task_count=task_count, enabled_count=enabled_count)
    _sched._write_live_state(
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
    _sched._notify_portal_state()

    load_failures = 0
    last_load_error: str | None = None

    while True:
        max_sleep = _sched._sched_config().max_loop_sleep

        try:
            board = _sched.load_board()
            load_failures = 0
            last_load_error = None
        except (FileNotFoundError, ValueError) as e:
            # The board was valid at startup but has since gone bad (mid-edit,
            # corruption). Back off exponentially with throttled logging rather
            # than spinning every loop and ballooning the log (#449).
            wait = _board_load_backoff(load_failures)
            msg = str(e)
            if msg != last_load_error:
                print(f"[{_ts()}] Board read error: {msg} — backing off "
                      f"{int(wait)}s", file=sys.stderr)
                _sched._log_event("board_load_error", error=msg[:200], backoff=wait,
                                  attempt=load_failures + 1)
                last_load_error = msg
            uptime = int((datetime.now(timezone.utc) - started_at).total_seconds())
            _sched._write_live_state(
                status="error",
                started_at=started_at.isoformat(),
                error=msg[:200],
                tasks_completed=tasks_completed,
                tasks_failed=tasks_failed,
                uptime_seconds=uptime,
            )
            load_failures += 1
            time.sleep(wait)
            continue

        # Reap worktrees whose PR has merged/closed (cheap — only tasks with
        # an open tracked PR hit `gh`).
        try:
            _sched.reap_worktree_prs(board)
        except Exception as e:
            print(f"[{_ts()}] Worktree reap error: {e}", file=sys.stderr)

        task_name, wait_seconds = _sched.pick_next_task(board)
        uptime = int((datetime.now(timezone.utc) - started_at).total_seconds())

        if task_name is None:
            sleep_time = min(wait_seconds, max_sleep)
            print(f"[{_ts()}] Nothing due. Sleeping {int(sleep_time)}s...")
            _sched._write_live_state(
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
            _sched._notify_portal_state()
            time.sleep(sleep_time)
            continue

        if wait_seconds > 0:
            sleep_time = min(wait_seconds, max_sleep)
            print(f"[{_ts()}] Next: {task_name} in {_sched.format_interval(int(wait_seconds))}. Sleeping {int(sleep_time)}s...")
            _sched._log_event("scheduler_sleeping", next_task=task_name,
                              sleep_seconds=round(sleep_time, 1))
            _sched._write_live_state(
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
            _sched._notify_portal_state()
            time.sleep(sleep_time)
            continue

        # Dispatch the task
        print(f"[{_ts()}] Running: {task_name}")
        task_started = datetime.now(timezone.utc)
        _sched._write_live_state(
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
        _sched._notify_portal_state()

        state = _sched.dispatch_task(board, task_name)
        board.state[task_name] = state
        _sched.save_board(board)

        if state.last_status == "complete":
            tasks_completed += 1
        elif state.last_status not in ("lock_conflict", "never"):
            tasks_failed += 1

        print(f"[{_ts()}] Done: {task_name} → {state.last_status} ({state.last_duration}s)")

        cooldown = _sched._sched_config().dispatch_cooldown
        if cooldown > 0:
            print(f"[{_ts()}] Cooldown: {cooldown}s")
            time.sleep(cooldown)

        # Periodic orphan sweep (every ~10 iterations)
        loop_count += 1
        if loop_count % 10 == 0:
            _sched._sweep_orphaned_processes()
