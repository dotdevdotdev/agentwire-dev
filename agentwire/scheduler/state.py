"""Board load/save and run-state persistence (atomic writes + backup rotation)."""

import os
import shutil
from pathlib import Path

import yaml

from ..core import _atomic_write
from .models import (
    Board,
    SchedulerTask,
    TaskState,
    _parse_datetime_field,
    _parse_schedule,
)


def load_board() -> Board:
    """Load the scheduler board from YAML.

    Returns:
        Board with tasks and state populated.

    Raises:
        FileNotFoundError: If board file doesn't exist.
        ValueError: If board file is malformed.
    """
    from agentwire import scheduler as _sched

    board_path = _sched._sched_config().board_file
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
            posture=t.get("posture"),
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

    # Run-state lives in its own file (scheduler-state.yaml). For boards that
    # still carry a legacy embedded `state:` block, read it too — but the
    # dedicated state file always wins on conflict. The daemon NEVER writes
    # state back into board_file (#449).
    merged_state: dict = {}
    legacy_state = raw.get("state", {})
    if isinstance(legacy_state, dict):
        merged_state.update(legacy_state)
    merged_state.update(_sched._load_state_file())

    if merged_state:
        for name, s in merged_state.items():
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


def _backup_path(path: Path, n: int) -> Path:
    """Path of the Nth rotated backup of `path` (e.g. scheduler-state.yaml.bak1)."""
    return path.with_name(path.name + f".bak{n}")


def _rotate_backups(path: Path, keep: int) -> None:
    """Rotate path -> .bak1 .. .bakN before it is overwritten.

    Only validated content is ever written to `path`, so every rotated copy
    is a known-good snapshot — a bad write is recoverable from the newest
    backup (#449). No-op if backups are disabled or there's nothing to rotate.
    """
    if keep <= 0 or not path.exists():
        return
    # Shift older backups down: .bak(N-1) -> .bakN, ..., .bak1 -> .bak2
    for i in range(keep, 1, -1):
        src = _backup_path(path, i - 1)
        if src.exists():
            os.replace(src, _backup_path(path, i))
    try:
        shutil.copy2(path, _backup_path(path, 1))
    except OSError:
        pass


def _state_to_dict(board: Board) -> dict:
    """Serialize board run-state to a plain dict for persistence."""
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
    return state_dict


def _load_state_file() -> dict:
    """Load run-state from the dedicated state file, healing through backups.

    Returns the `state:` mapping. If the live file is missing or won't parse,
    falls back through the rotated backups (newest first) so one bad write
    doesn't lose history. Returns {} if nothing parses.
    """
    from agentwire import scheduler as _sched

    cfg = _sched._sched_config()
    state_path = cfg.state_file
    keep = getattr(cfg, "state_backups", 5)
    candidates = [state_path] + [_backup_path(state_path, i) for i in range(1, keep + 1)]
    for path in candidates:
        if not path.exists():
            continue
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except (yaml.YAMLError, OSError):
            continue
        if isinstance(data, dict):
            st = data.get("state", {})
            if isinstance(st, dict):
                return st
    return {}


def save_board(board: Board) -> None:
    """Persist run-state to the dedicated state file.

    NEVER touches board_file — that holds user-authored task definitions and
    is read-only to the daemon (#449). Writes are atomic (temp + fsync +
    re-parse validation + rename) and the previous good copy is rotated into
    a backup first, so a bad write can never clobber the board or itself.
    """
    from agentwire import scheduler as _sched

    state_path = _sched._sched_config().state_file
    payload = {"state": _state_to_dict(board)}
    text = _sched.yaml.dump(payload, default_flow_style=False, sort_keys=False)

    def _validate(tmp_path: str) -> None:
        with open(tmp_path) as f:
            reparsed = yaml.safe_load(f)
        if not isinstance(reparsed, dict) or "state" not in reparsed:
            raise ValueError("scheduler state file failed re-parse validation")

    _rotate_backups(state_path, _sched._sched_config().state_backups)
    _atomic_write(state_path, text, validate=_validate)
