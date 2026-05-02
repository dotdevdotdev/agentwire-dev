"""Overnight session queue — prepare once, fork many, execute overnight.

Human prepares sessions interactively during the day, queues them, and
the overnight orchestrator dispatches them autonomously during off-hours
with quota management and PR creation.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Paths

OVERNIGHT_DIR = Path.home() / ".agentwire" / "overnight"
DONE_DIR = OVERNIGHT_DIR / "done"


# Data

@dataclass
class OvernightItem:
    """A single queued overnight session."""

    id: str
    description: str
    session: str               # Tmux session name for execution (overnight-<id>)
    source_session: str        # Original session that was prepared
    resume_session_id: str     # Claude sessionId for --resume --fork-session
    project_path: str          # Absolute path to project
    parent_branch: str         # Git branch at preparation time
    parent_commit: str         # Git HEAD at preparation time
    work_branch: str           # overnight/<id>-<slug>
    pr_target: str             # Branch to PR against
    session_type: str          # Session type for execution
    status: str = "queued"     # queued | running | complete | failed | cancelled
    priority: int = 50         # Lower = higher priority
    prepared_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    pr_url: Optional[str] = None
    summary: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> OvernightItem:
        # Accept extra keys gracefully
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


# Helpers

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def slugify(text: str, max_len: int = 40) -> str:
    """Convert description to branch-safe slug."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s-]+", "-", s)
    s = s.strip("-")
    return s[:max_len]


def generate_id() -> str:
    """Generate a short unique ID."""
    return uuid.uuid4().hex[:6]


def _inject_resume_flags(agent_cmd: str, session_type: str, resume_session_id: str) -> str:
    """Insert `--resume <id> --fork-session` after the `claude` binary token.

    Phase 5: claude-* only. Other session types (sdk-*, pi-*) have their
    own resume conventions and the claude-style UUID we record doesn't apply.
    A no-op when there's nothing to resume or the type doesn't qualify.

    The previous form used `rfind("claude")` which was buggy for commands
    that also carry `--model claude-opus-4-7` — it would match inside the
    model arg and produce a malformed command.
    """
    if not resume_session_id or not session_type.startswith("claude"):
        return agent_cmd
    if not agent_cmd.startswith("claude "):
        return agent_cmd
    insert_pos = len("claude")
    return (
        agent_cmd[:insert_pos]
        + f" --resume {resume_session_id} --fork-session"
        + agent_cmd[insert_pos:]
    )


# Queue CRUD

def _ensure_dirs():
    OVERNIGHT_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)


def save_item(item: OvernightItem) -> None:
    _ensure_dirs()
    path = OVERNIGHT_DIR / f"{item.id}.json"
    fd, tmp = tempfile.mkstemp(dir=str(OVERNIGHT_DIR), suffix=".tmp")
    try:
        with open(fd, "w") as f:
            json.dump(item.to_dict(), f, indent=2)
        Path(tmp).rename(path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_item(item_id: str) -> Optional[OvernightItem]:
    path = OVERNIGHT_DIR / f"{item_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return OvernightItem.from_dict(data)
    except (json.JSONDecodeError, TypeError):
        return None


def load_queue(status: Optional[str] = None) -> list[OvernightItem]:
    """Load all queue items, optionally filtered by status."""
    _ensure_dirs()
    items = []
    for path in OVERNIGHT_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            item = OvernightItem.from_dict(data)
            if status is None or item.status == status:
                items.append(item)
        except (json.JSONDecodeError, TypeError):
            continue
    return items


def load_done() -> list[OvernightItem]:
    """Load completed/archived items from done/."""
    _ensure_dirs()
    items = []
    for path in DONE_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            items.append(OvernightItem.from_dict(data))
        except (json.JSONDecodeError, TypeError):
            continue
    return items


def archive_item(item: OvernightItem) -> None:
    """Move item to done/ directory."""
    _ensure_dirs()
    src = OVERNIGHT_DIR / f"{item.id}.json"
    dst = DONE_DIR / f"{item.id}.json"
    fd, tmp = tempfile.mkstemp(dir=str(DONE_DIR), suffix=".tmp")
    try:
        with open(fd, "w") as f:
            json.dump(item.to_dict(), f, indent=2)
        Path(tmp).rename(dst)
        src.unlink(missing_ok=True)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def delete_item(item_id: str) -> bool:
    """Remove an item from the queue entirely."""
    path = OVERNIGHT_DIR / f"{item_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# Session ID resolution (extracted from cmd_fork logic)

def _resolve_claude_session_id(session_name: str, project_path: str) -> Optional[str]:
    """Find the Claude conversation sessionId for a tmux session.

    Uses history.jsonl to match by project path and tmux session creation time.
    """
    # Get tmux session creation timestamp
    result = subprocess.run(
        ["tmux", "display-message", "-t", session_name, "-p", "#{session_created}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None

    session_created_unix = int(result.stdout.strip() or 0)
    session_created_ms = session_created_unix * 1000

    history_file = Path.home() / ".claude" / "history.jsonl"
    if session_created_ms > 0 and history_file.exists():
        first_seen: dict[str, int] = {}
        for line in history_file.read_text().strip().splitlines():
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("project") != project_path:
                    continue
                sid = entry.get("sessionId", "")
                ts = entry.get("timestamp", 0)
                if sid and (sid not in first_seen or ts < first_seen[sid]):
                    first_seen[sid] = ts
            except json.JSONDecodeError:
                continue

        # Pick session with earliest first_seen >= session_created_ms
        candidates = [(sid, ts) for sid, ts in first_seen.items() if ts >= session_created_ms]
        if candidates:
            candidates.sort(key=lambda x: x[1])
            return candidates[0][0]

    # Fallback: most recently modified JSONL in project dir
    try:
        from .history import encode_project_path
        claude_projects_dir = Path.home() / ".claude" / "projects"
        encoded_path = encode_project_path(project_path)
        session_dir = claude_projects_dir / encoded_path
        if session_dir.exists():
            jsonl_files = sorted(
                session_dir.glob("*.jsonl"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            if jsonl_files:
                return jsonl_files[0].stem
    except Exception:
        pass

    return None


def _get_session_cwd(session_name: str) -> Optional[str]:
    """Get the current working directory of a tmux session's pane 0."""
    result = subprocess.run(
        ["tmux", "display-message", "-t", f"{session_name}.0", "-p", "#{pane_current_path}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _get_git_branch(project_path: str) -> Optional[str]:
    """Get current git branch."""
    result = subprocess.run(
        ["git", "-C", project_path, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _get_git_head(project_path: str) -> Optional[str]:
    """Get current git HEAD commit hash."""
    result = subprocess.run(
        ["git", "-C", project_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


# Preparation

def prepare_item(
    source_session: str,
    description: str,
    priority: int = 50,
    session_type: Optional[str] = None,
    branch_prefix: str = "overnight/",
) -> OvernightItem:
    """Create a new overnight queue item from an active session.

    Resolves the session's project path, git state, and Claude conversation ID
    so it can be forked and dispatched later.
    """
    from .config import get_config
    config = get_config()

    if session_type is None:
        session_type = config.overnight.session_type

    # Resolve source session details
    project_path = _get_session_cwd(source_session)
    if not project_path:
        raise RuntimeError(f"Cannot determine CWD for session '{source_session}'")

    branch = _get_git_branch(project_path)
    if not branch:
        raise RuntimeError(f"Cannot determine git branch in {project_path}")

    commit = _get_git_head(project_path)
    if not commit:
        raise RuntimeError(f"Cannot determine git HEAD in {project_path}")

    resume_id = _resolve_claude_session_id(source_session, project_path)
    if not resume_id:
        raise RuntimeError(
            f"Cannot find Claude session ID for '{source_session}'. "
            "Is Claude running in this session?"
        )

    item_id = generate_id()
    slug = slugify(description)
    work_branch = f"{branch_prefix}{item_id}-{slug}" if slug else f"{branch_prefix}{item_id}"

    item = OvernightItem(
        id=item_id,
        description=description,
        session=f"overnight-{item_id}",
        source_session=source_session,
        resume_session_id=resume_id,
        project_path=project_path,
        parent_branch=branch,
        parent_commit=commit,
        work_branch=work_branch,
        pr_target=branch,
        session_type=session_type,
        priority=priority,
        prepared_at=datetime.now(timezone.utc).isoformat(),
    )
    save_item(item)
    return item


# Event logging & live state

def _get_overnight_config():
    from .config import get_config
    return get_config().overnight


def _log_event(event: str, **fields) -> None:
    """Append an event to the overnight JSONL log."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    try:
        events_path = _get_overnight_config().events_file
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with open(events_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _write_live_state(**fields) -> None:
    """Atomically write the overnight live state JSON file."""
    try:
        live_path = _get_overnight_config().live_state_file
        live_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(live_path.parent), suffix=".tmp")
        try:
            with open(fd, "w") as f:
                json.dump(fields, f, indent=2)
            Path(tmp).rename(live_path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        pass


def read_live_state() -> Optional[dict]:
    """Read the overnight live state file."""
    try:
        live_path = _get_overnight_config().live_state_file
        if live_path.exists():
            return json.loads(live_path.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return None


def read_events(tail: int = 20) -> list[dict]:
    """Read recent overnight events."""
    try:
        events_path = _get_overnight_config().events_file
        if not events_path.exists():
            return []
        lines = events_path.read_text().strip().splitlines()
        entries = []
        for line in lines[-tail:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except OSError:
        return []


# Notify portal

def _notify_portal(item_id: str, status: str, summary: str = "") -> None:
    """Notify portal of overnight item state change."""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        from .config import get_config
        portal_url = get_config().portal.url

        requests.post(
            f"{portal_url}/api/notify",
            json={
                "event": "overnight_item_complete",
                "item_id": item_id,
                "status": status,
                "summary": summary,
            },
            verify=False,
            timeout=5,
        )
    except Exception:
        pass


# Orchestrator: dispatch, completion, finalization

def _tmux_session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"],
        capture_output=True,
    )
    return result.returncode == 0


def in_overnight_window(config) -> bool:
    """Check if current time is within the overnight work window."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    if config.timezone:
        tz = ZoneInfo(config.timezone)
    else:
        tz = None  # Use local timezone

    now = datetime.now(tz)
    current_minutes = now.hour * 60 + now.minute

    start_h, start_m = map(int, config.window_start.split(":"))
    end_h, end_m = map(int, config.window_end.split(":"))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if start_minutes > end_minutes:
        # Window crosses midnight (e.g., 22:00 - 07:00)
        return current_minutes >= start_minutes or current_minutes < end_minutes
    else:
        # Window within same day (e.g., 01:00 - 06:00)
        return start_minutes <= current_minutes < end_minutes


def pick_next(items: list[OvernightItem]) -> Optional[OvernightItem]:
    """Pick the next item to dispatch, sorted by priority then prepared_at."""
    queued = [i for i in items if i.status == "queued"]
    if not queued:
        return None
    queued.sort(key=lambda i: (i.priority, i.prepared_at))
    return queued[0]


def dispatch_item(item: OvernightItem, config) -> bool:
    """Create a tmux session and dispatch the overnight item.

    1. Create tmux session in project directory
    2. Launch agent with forked conversation context
    3. Wait for agent ready
    4. Create work branch
    5. Send go prompt
    6. Write task context for idle hook integration

    Returns True if dispatch succeeded.
    """
    from .__main__ import build_agent_command, _wait_for_agent_ready, _build_tmux_env_flags

    project = item.project_path
    session = item.session

    # Verify project exists
    if not Path(project).is_dir():
        item.status = "failed"
        item.error = f"Project directory not found: {project}"
        save_item(item)
        _log_event("dispatch_failed", item_id=item.id, error=item.error)
        return False

    # Kill existing session if any
    if _tmux_session_exists(session):
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)

    # Build agent command with forked context (before session creation, so we
    # can inject env via `tmux new-session -e K=V` — post-hoc set-environment
    # doesn't reach the initial shell).
    agent = build_agent_command(item.session_type)
    agent_cmd = agent.command
    if not agent_cmd:
        item.status = "failed"
        item.error = f"No agent command for session type: {item.session_type}"
        save_item(item)
        _log_event("dispatch_failed", item_id=item.id, error=item.error)
        return False

    # Create tmux session with env injected at creation time
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", project,
         *_build_tmux_env_flags(agent.env)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        item.status = "failed"
        item.error = f"Failed to create tmux session: {result.stderr}"
        save_item(item)
        _log_event("dispatch_failed", item_id=item.id, error=item.error)
        return False

    agent_cmd = _inject_resume_flags(agent_cmd, item.session_type, item.resume_session_id)

    # Launch agent
    subprocess.run(
        ["tmux", "send-keys", "-t", session, agent_cmd, "Enter"],
        capture_output=True,
    )

    # Wait for agent to be ready
    print(f"[{_ts()}] Waiting for agent in {session}...")
    if not _wait_for_agent_ready(session, timeout=60):
        item.status = "failed"
        item.error = "Agent did not become ready within 60s"
        save_item(item)
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        _log_event("dispatch_failed", item_id=item.id, error=item.error)
        return False

    # Create work branch
    branch_cmd = f"git checkout -b {shlex.quote(item.work_branch)}"
    subprocess.run(
        ["tmux", "send-keys", "-t", session, branch_cmd, "Enter"],
        capture_output=True,
    )
    time.sleep(2)  # Let branch checkout complete

    # Send go prompt
    go_prompt = config.go_prompt.replace("\n", " ").strip()
    subprocess.run(
        ["tmux", "send-keys", "-t", session, go_prompt, "Enter"],
        capture_output=True,
    )

    # Write task context for idle hook
    try:
        from .completion import write_task_context
        summary_file = str(Path(project) / f".agentwire/overnight-{item.id}.md")
        write_task_context(
            session=session,
            task_name=f"overnight-{item.id}",
            summary_file=summary_file,
            exit_on_complete=True,
        )
    except Exception as e:
        print(f"[{_ts()}] Warning: could not write task context: {e}")

    # Update item state
    item.status = "running"
    item.started_at = datetime.now(timezone.utc).isoformat()
    save_item(item)

    _log_event("dispatched", item_id=item.id, session=session, description=item.description)
    print(f"[{_ts()}] Dispatched: {item.id} ({item.description})")
    return True


def check_completion(item: OvernightItem, config) -> str:
    """Check if a running overnight item has completed.

    Returns the new status: 'running', 'complete', 'failed', or 'stuck'.
    """
    from .completion import _session_has_agent

    session = item.session

    # Session gone entirely
    if not _tmux_session_exists(session):
        return "complete"

    # Agent still running?
    if _session_has_agent(session):
        # Check timeout
        if item.started_at:
            started = datetime.fromisoformat(item.started_at)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            if elapsed > config.session_timeout:
                return "stuck"
        return "running"

    # Agent not running = session idle = task complete or failed
    return "complete"


def finalize_item(item: OvernightItem, config) -> None:
    """Finalize a completed overnight item: commit, push, PR, archive.

    1. Auto-commit uncommitted changes
    2. Push work branch
    3. Create draft PR
    4. Parse summary if exists
    5. Kill session
    6. Checkout parent branch
    7. Archive item
    """
    project = item.project_path
    session = item.session
    git_timeout = 15

    # Parse summary file if exists
    summary_file = Path(project) / f".agentwire/overnight-{item.id}.md"
    if summary_file.exists():
        try:
            from .completion import parse_summary_file
            result = parse_summary_file(summary_file)
            item.summary = result.summary
            if result.status == "failed":
                item.status = "failed"
            else:
                item.status = "complete"
        except Exception:
            item.status = "complete"
    else:
        item.status = "complete"

    # Auto-commit changes
    check = subprocess.run(
        ["git", "-C", project, "status", "--porcelain"],
        capture_output=True, text=True, timeout=git_timeout,
    )
    if check.returncode == 0 and check.stdout.strip():
        subprocess.run(
            ["git", "-C", project, "add", "-A"],
            capture_output=True, timeout=git_timeout,
        )
        # Protect project config from agent modifications
        subprocess.run(
            ["git", "-C", project, "reset", "HEAD", "--", ".agentwire.yml"],
            capture_output=True, timeout=git_timeout,
        )
        subprocess.run(
            ["git", "-C", project, "checkout", "--", ".agentwire.yml"],
            capture_output=True, timeout=git_timeout,
        )
        # Check if anything still staged
        staged = subprocess.run(
            ["git", "-C", project, "diff", "--cached", "--quiet"],
            capture_output=True, timeout=git_timeout,
        )
        if staged.returncode != 0:
            msg = f"overnight: {item.id} ({item.description})"
            subprocess.run(
                ["git", "-C", project, "commit", "-m", msg, "--no-verify"],
                capture_output=True, text=True, timeout=git_timeout,
            )
            print(f"[{_ts()}] Auto-committed: {msg}")

    # Push work branch
    push = subprocess.run(
        ["git", "-C", project, "push", "-u", "origin", item.work_branch],
        capture_output=True, text=True, timeout=60,
    )
    if push.returncode != 0:
        print(f"[{_ts()}] Warning: push failed: {push.stderr}")

    # Create draft PR
    pr_url = None
    if push.returncode == 0:
        pr_args = [
            "gh", "pr", "create",
            "--base", item.pr_target,
            "--head", item.work_branch,
            "--title", f"[overnight] {item.description}",
            "--body", f"Overnight session `{item.id}` — {item.description}\n\n"
                      f"Prepared from session `{item.source_session}` on branch `{item.parent_branch}`.\n\n"
                      f"Summary: {item.summary or 'No summary available.'}\n\n"
                      f"Built by [dotdev.dev](https://dotdev.dev)",
        ]
        if config.pr_draft:
            pr_args.append("--draft")
        pr_result = subprocess.run(
            pr_args,
            capture_output=True, text=True, timeout=30,
            cwd=project,
        )
        if pr_result.returncode == 0:
            pr_url = pr_result.stdout.strip()
            item.pr_url = pr_url
            print(f"[{_ts()}] PR created: {pr_url}")
        else:
            print(f"[{_ts()}] Warning: PR creation failed: {pr_result.stderr}")

    # Kill session
    if _tmux_session_exists(session):
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)

    # Checkout parent branch
    subprocess.run(
        ["git", "-C", project, "checkout", item.parent_branch],
        capture_output=True, text=True, timeout=git_timeout,
    )

    # Clean up summary file
    summary_file.unlink(missing_ok=True)

    # Update and archive
    item.completed_at = datetime.now(timezone.utc).isoformat()
    archive_item(item)

    _log_event(
        "finalized",
        item_id=item.id,
        status=item.status,
        pr_url=pr_url,
        summary=item.summary,
    )
    _notify_portal(item.id, item.status, item.summary or "")

    print(f"[{_ts()}] Finalized: {item.id} → {item.status}")
    if pr_url:
        print(f"  PR: {pr_url}")


def _handle_stuck(item: OvernightItem, config) -> None:
    """Handle a stuck overnight item (timed out)."""
    session = item.session

    # Kill session
    if _tmux_session_exists(session):
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)

    item.status = "failed"
    item.error = f"Session timed out after {config.session_timeout}s"
    item.completed_at = datetime.now(timezone.utc).isoformat()

    # Checkout parent branch
    subprocess.run(
        ["git", "-C", item.project_path, "checkout", item.parent_branch],
        capture_output=True, text=True, timeout=15,
    )

    archive_item(item)
    _log_event("stuck", item_id=item.id, error=item.error)
    _notify_portal(item.id, "failed", item.error)
    print(f"[{_ts()}] STUCK: {item.id} — {item.error}")


# Orchestrator loop

def run_overnight_loop() -> None:
    """Main overnight orchestrator loop."""
    config = _get_overnight_config()
    running = True

    def _shutdown(sig, frame):
        nonlocal running
        print(f"\n[{_ts()}] Shutting down overnight orchestrator...")
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"[{_ts()}] Overnight orchestrator started")
    print(f"  Window: {config.window_start} - {config.window_end}")
    print(f"  Max concurrent: {config.max_concurrent}")
    print(f"  Session type: {config.session_type}")
    print(f"  Check interval: {config.check_interval}s")
    _log_event("started")

    while running:
        try:
            items = load_queue()
            running_items = [i for i in items if i.status == "running"]
            queued_items = [i for i in items if i.status == "queued"]

            # Update live state
            _write_live_state(
                running=True,
                in_window=in_overnight_window(config),
                queued=len(queued_items),
                active=len(running_items),
                active_items=[i.to_dict() for i in running_items],
                ts=datetime.now(timezone.utc).isoformat(),
            )

            # Check completion of running items
            for item in running_items:
                status = check_completion(item, config)
                if status == "complete":
                    finalize_item(item, config)
                elif status == "stuck":
                    _handle_stuck(item, config)
                # else: still running

            # Only dispatch during overnight window
            if not in_overnight_window(config):
                time.sleep(config.check_interval)
                continue

            # Reload after finalization may have changed things
            items = load_queue()
            running_items = [i for i in items if i.status == "running"]
            queued_items = [i for i in items if i.status == "queued"]

            # Dispatch if under max_concurrent
            if len(running_items) < config.max_concurrent:
                next_item = pick_next(items)
                if next_item:
                    dispatch_item(next_item, config)

            # Check if all done
            items = load_queue()
            if not any(i.status in ("queued", "running") for i in items):
                if queued_items or running_items:
                    # Just finished everything
                    _log_event("all_complete")
                    _on_all_complete(config)

        except Exception as e:
            print(f"[{_ts()}] Error in overnight loop: {e}")
            _log_event("error", error=str(e))

        time.sleep(config.check_interval)

    # Clean shutdown
    _write_live_state(running=False, ts=datetime.now(timezone.utc).isoformat())
    _log_event("stopped")
    print(f"[{_ts()}] Overnight orchestrator stopped")


def _on_all_complete(config) -> None:
    """Handle all overnight items completing."""
    print(f"[{_ts()}] All overnight items complete!")

    # Voice notification
    try:
        subprocess.run(
            ["agentwire", "say", "All overnight sessions have completed. Check your draft PRs."],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass
