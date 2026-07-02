"""Centralized task scheduler daemon.

Reads a board of registered tasks from ~/.agentwire/scheduler.yaml,
picks the most overdue one, dispatches it via `agentwire ensure`,
updates the board, and loops. No AI — pure subprocess management
and time math.

Split into cohesive submodules (#633): models (dataclasses/constants/parsing),
state (board load/save + backups), schedule (eligibility math + picking),
gates (precondition checks), dispatch (ensure/worktree+PR execution),
report (events/live-state/portal/display), loop (the daemon).

This package IS the public module path: every symbol keeps its historical
``agentwire.scheduler`` home, and submodules resolve cross-cutting calls
through this namespace at call time so ``patch("agentwire.scheduler.X")``
keeps working exactly as it did on the monolith.
"""
# ruff: noqa: F401

import subprocess
import time
from datetime import datetime, timezone

import yaml

from ..core import _atomic_write
from .dispatch import (
    _WORKFLOW_STATUS_TO_SCHED,
    _apply_max_runs,
    _capture_head,
    _collect_descendants,
    _dispatch_ensure_task,
    _dispatch_inplace_task,
    _dispatch_worktree_task,
    _finalize_worktree_pr,
    _is_git_repo,
    _is_worktree_task,
    _kill_process_tree,
    _kill_session,
    _parse_ensure_summary,
    _pr_number_from_url,
    _pr_state,
    _pre_create_session,
    _remove_scheduler_worktree,
    _run_ensure,
    _sweep_orphaned_processes,
    _task_is_persistent,
    _unattended_env,
    dispatch_task,
    reap_worktree_prs,
)
from .gates import _check_gate, _clear_gate_error, _gate_errored, _gated_tasks
from .loop import _board_load_backoff, _load_board_blocking, run_scheduler_loop
from .models import (
    _CALENDAR_EVERY,
    _DAY_NAMES,
    _EXIT_COMPLETE,
    _EXIT_FAILED,
    _EXIT_INCOMPLETE,
    _EXIT_LOCK_CONFLICT,
    _EXIT_PRE_FAILURE,
    _EXIT_SESSION_ERROR,
    _EXIT_TIMEOUT,
    _EXIT_TO_STATUS,
    _EXIT_USAGE_LIMIT,
    _IN_FLIGHT_GRACE,
    SCHEDULER_SESSION,
    Board,
    Schedule,
    SchedulerTask,
    TaskState,
    _parse_datetime_field,
    _parse_duration,
    _parse_schedule,
    _parse_time,
    _sched_config,
    _ts,
)
from .report import (
    _log_event,
    _notify_portal,
    _notify_portal_state,
    _write_live_state,
    format_interval,
    format_overdue,
    format_schedule,
    get_board_display,
    read_events,
    read_live_state,
)
from .schedule import (
    _compute_next_eligible,
    _compute_recurrence,
    _day_matches,
    _dt_to_ts,
    _get_last_run_ts,
    _in_time_window,
    _is_in_flight,
    _validate_task_payload,
    pick_next_task,
    seconds_until_next_due,
    validate_board,
)
from .state import (
    _backup_path,
    _load_state_file,
    _rotate_backups,
    _state_to_dict,
    load_board,
    save_board,
)
