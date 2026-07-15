"""Gate evaluation — precondition checks that decide whether a task runs."""

import subprocess

from .models import TaskState, _ts

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


def _check_gate(board, task_name: str) -> bool:
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
    from agentwire import scheduler as _sched

    task = board.tasks[task_name]
    gate = task.gate
    if not gate or not isinstance(gate, dict):
        _gated_tasks.discard(task_name)
        _clear_gate_error(board, task_name)
        return True

    cfg = _sched._sched_config()
    state = board.state.get(task_name, TaskState())
    project = task.project

    def _gate_skip(gate_type: str, reason: str, **extra):
        """Record a gate skip. Logs only on first occurrence (spam control),
        but persists ``last_gate_skip`` on every REASON CHANGE.

        Distinct from ``_gate_error`` below: this is the CLEAN "not ready
        yet" path (e.g. a ``command`` gate legitimately returning nonzero),
        not an exception. Recorded so the board can show "waiting on gate"
        instead of reading as silently-falling-behind overdue (#803).
        Persisting on reason-change (not just first-occurrence, which
        ``_gated_tasks`` alone gates) matters for multi-condition gates: if
        task's blocker shifts from ``git_commit`` to ``command`` without the
        gate ever fully passing in between, ``task_name`` never leaves
        ``_gated_tasks``, so a first-occurrence-only write would leave the
        board showing the ORIGINAL, now-stale reason.
        """
        skip_text = f"{gate_type}: {reason}"
        if state.last_gate_skip != skip_text:
            state.last_gate_skip = skip_text
            board.state[task_name] = state
            _sched.save_board(board)
        if task_name not in _gated_tasks:
            _sched._log_event("task_gated", task=task_name, gate_type=gate_type,
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
            _sched._log_event("gate_error", task=task_name, gate_type=gate_type,
                              reason=reason)
            print(f"[{_ts()}] Gate error on {task_name}: {gate_type} "
                  f"({reason}) — failing open")
            state.last_gate_error = f"{gate_type}: {reason}"
            state.last_gate_skip = ""  # failing open — no longer "waiting on gate"
            board.state[task_name] = state
            _sched.save_board(board)
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


def _clear_gate_error(board, task_name: str) -> None:
    """Clear a previously-recorded gate error/skip once the gate evaluates cleanly.

    No-op (and no board write) unless this task actually had something
    recorded, so the common clean path stays cheap. Clears BOTH
    ``last_gate_error`` (exception path) and ``last_gate_skip`` (clean
    "not ready yet" path, #803) — both are stale the moment the gate passes.
    """
    from agentwire import scheduler as _sched

    state = board.state.get(task_name)
    had_tracked = _gate_errored.pop(task_name, None) is not None
    had_state = state is not None and (state.last_gate_error or state.last_gate_skip)
    if had_tracked or had_state:
        if state is not None and (state.last_gate_error or state.last_gate_skip):
            state.last_gate_error = ""
            state.last_gate_skip = ""
            _sched.save_board(board)
