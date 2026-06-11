"""Completion detection for scheduled tasks.

Handles:
- Task context files (coordinate with idle hook)
- System summary prompt (ask agent to write summary)
- Summary file parsing (extract status from YAML front matter)
- Completion signal files (hook signals ensure)
"""

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple


class CompletionError(Exception):
    """Raised when completion detection fails."""

    pass


class CompletionTimeout(CompletionError):
    """Raised when waiting for completion times out."""

    pass


# Directory for task coordination files
TASKS_DIR = Path.home() / ".agentwire" / "tasks"

# Shells that indicate agent died and fell back to bare shell
_BARE_SHELLS = {"zsh", "bash", "sh", "fish", "tcsh", "csh"}


def _session_has_agent(session: str) -> bool:
    """Check if session exists and has an agent running in any pane."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", f"={session}", "-F", "#{pane_current_command}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False  # Session doesn't exist

    for line in result.stdout.strip().split("\n"):
        if line.strip().lower() not in _BARE_SHELLS:
            return True

    return False


class SummaryResult(NamedTuple):
    """Parsed result from a task summary file."""

    status: str  # complete, incomplete, failed
    summary: str  # One-line summary
    files_modified: list[str]  # List of modified files
    blockers: list[str]  # List of blockers (if any)
    raw_content: str  # Full file content


# System prompt sent after task completion to get structured summary
SYSTEM_SUMMARY_PROMPT = """Write a task summary to {summary_file} in YAML front matter format:

```markdown
---
status: complete | incomplete | failed
summary: one line describing what you accomplished
files_modified:
  - path/to/file1
  - path/to/file2
blockers:
  - any issues preventing completion
---

Additional notes about what was done, challenges encountered, etc.
```

Status meanings:
- complete: Task finished successfully
- incomplete: Task partially done, more work needed (not a failure)
- failed: Task could not be completed due to errors

Write the file now."""


def generate_summary_filename(session: str, task_name: str) -> str:
    """Generate a session-scoped timestamped summary filename.

    Includes session name so multiple sessions sharing a project directory
    don't collide on summary files or trigger false TASK-ORPHAN detection.

    Args:
        session: tmux session name
        task_name: Task name (for context)

    Returns:
        Relative path like .agentwire/task-summary-mysession-2024-01-15T07-00-00.md
    """
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    return f".agentwire/task-summary-{session}-{task_name}-{timestamp}.md"


# =============================================================================
# Task Context (coordinate between ensure and idle hook)
# =============================================================================


def write_task_context(
    session: str,
    task_name: str,
    summary_file: str,
    attempt: int = 1,
    exit_on_complete: bool = True,
    mode: str = "standard",
    max_iterations: int = 3,
    iteration: int = 1,
    loop_review: bool = True,
    loop_delay: int = 0,
    original_prompt: str = "",
) -> Path:
    """Write task context file for hook coordination.

    The idle hook reads this to know:
    - A scheduled task is running
    - What summary file to request
    - Whether to exit the session after completion
    - Loop mode configuration (mode, iteration count, review flag, delay)

    Args:
        session: tmux session name
        task_name: Task being executed
        summary_file: Relative path for summary file
        attempt: Current attempt number
        exit_on_complete: Whether to exit session after task completion
        mode: Task mode ("standard" or "loop")
        max_iterations: Maximum loop iterations (loop mode only)
        iteration: Current iteration number (loop mode only)
        loop_review: Whether to write review files between iterations
        loop_delay: Seconds to wait between loop iterations (loop mode only)
        original_prompt: Fully expanded task prompt (for re-sending in loop mode)

    Returns:
        Path to the context file
    """
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    context = {
        "task": task_name,
        "summary_file": summary_file,
        "started_at": datetime.now().isoformat(),
        "attempt": attempt,
        "idle_count": 0,  # Hook increments this
        "exit_on_complete": exit_on_complete,
        "mode": mode,
        "max_iterations": max_iterations,
        "iteration": iteration,
        "loop_review": loop_review,
        "loop_delay": loop_delay,
        "original_prompt": original_prompt,
    }

    context_file = TASKS_DIR / f"{session}.json"
    # Worktree session names contain a slash (e.g. "proj/branch"), which
    # nests the context file one directory down — create it.
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_text(json.dumps(context, indent=2))
    return context_file


def read_task_context(session: str) -> dict | None:
    """Read task context file.

    Args:
        session: tmux session name

    Returns:
        Context dict or None if not found
    """
    context_file = TASKS_DIR / f"{session}.json"
    if not context_file.exists():
        return None

    try:
        return json.loads(context_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def update_task_context(session: str, **updates) -> bool:
    """Update fields in task context file.

    Args:
        session: tmux session name
        **updates: Fields to update

    Returns:
        True if updated successfully
    """
    context = read_task_context(session)
    if context is None:
        return False

    context.update(updates)
    context_file = TASKS_DIR / f"{session}.json"

    try:
        context_file.write_text(json.dumps(context, indent=2))
        return True
    except OSError:
        return False


def clear_task_context(session: str) -> None:
    """Remove task context and completion signal files.

    Args:
        session: tmux session name
    """
    context_file = TASKS_DIR / f"{session}.json"
    complete_file = TASKS_DIR / f"{session}.complete"

    for f in [context_file, complete_file]:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


def wait_for_completion_signal(
    session: str,
    poll_interval: float = 10.0,
    summary_path: Path | None = None,
) -> dict:
    """Wait for task completion by polling the summary file directly.

    Exits when:
    1. Summary file appears (task completed normally)
    2. Session dies (agent crashed, tmux killed)

    There is no wall-clock timeout. Tasks run until one of these conditions.

    Args:
        session: tmux session name
        poll_interval: Seconds between checks
        summary_path: Path to the summary .md file the agent will write

    Returns:
        Dict with 'status', 'summary', 'summary_file' keys

    Raises:
        CompletionTimeout: If session dies before task completed
    """
    complete_file = TASKS_DIR / f"{session}.complete"

    # Build glob pattern for fuzzy summary detection (agents sometimes
    # invent their own timestamp instead of using the provided filename).
    summary_glob = None
    if summary_path:
        # e.g. task-summary-scheduler-daily-book-report-daily-report-*.md
        stem = summary_path.stem  # without .md
        # Strip the timestamp suffix (last 19 chars: YYYY-MM-DDTHH-MM-SS)
        prefix = stem[:-19] if len(stem) > 19 else stem
        summary_glob = f"{prefix}*.md"

    while True:
        # Primary: check if the agent has written the summary file AND
        # the hook has deleted the context file (signals cleanup complete).
        # This prevents ensure from proceeding before the hook finishes
        # its second-idle cleanup (send /exit, kill session).
        context_file = TASKS_DIR / f"{session}.json"

        # Check exact path first, then glob for nearby matches
        found_summary = None
        if summary_path and summary_path.exists() and summary_path.stat().st_size > 0:
            found_summary = summary_path
        elif summary_path and summary_glob:
            # Agent may have written a different timestamp — find newest match
            parent = summary_path.parent
            candidates = sorted(
                parent.glob(summary_glob),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                newest = candidates[0]
                # Only accept if created after ensure started (context file mtime)
                if context_file.exists():
                    ctx_mtime = context_file.stat().st_mtime
                    if newest.stat().st_mtime > ctx_mtime and newest.stat().st_size > 0:
                        found_summary = newest

        if found_summary and not context_file.exists():
            # Give a moment for the file to be fully written
            time.sleep(0.5)
            try:
                result = parse_summary_file(found_summary)
                return {
                    "status": result.status,
                    "summary": result.summary,
                    "summary_file": str(found_summary),
                }
            except CompletionError:
                pass  # File may be partially written, retry

        # Fallback: check for legacy .complete signal file
        if complete_file.exists():
            try:
                signal = json.loads(complete_file.read_text())
                return signal
            except (json.JSONDecodeError, OSError):
                pass  # File may be partially written, retry

        # Usage-limit dialog: park deterministically (zero-LLM) and report a
        # distinct status — the watchdog resumes the session after reset.
        # Also honors a park the watchdog performed first.
        from .usage_limit import check_and_park

        if check_and_park(session, source="ensure"):
            return {
                "status": "usage_limit",
                "summary": "Session parked on usage limit; auto-resumes after reset",
            }

        # Session gone or agent crashed (fell back to bare shell)
        if not _session_has_agent(session):
            raise CompletionTimeout(
                f"Session '{session}' died or agent exited before task completed"
            )

        time.sleep(poll_interval)


def get_summary_prompt(summary_file: str) -> str:
    """Get the system summary prompt with the filename filled in.

    Args:
        summary_file: Path to the summary file to create

    Returns:
        Complete prompt string
    """
    return SYSTEM_SUMMARY_PROMPT.format(summary_file=summary_file)


def is_session_idle(session: str, idle_threshold: float = 3.0) -> bool:
    """Check if a session is currently idle.

    Uses the portal's cached status if available, otherwise checks tmux activity.

    Args:
        session: Session name
        idle_threshold: Seconds of inactivity to consider idle

    Returns:
        True if session is idle, False otherwise
    """
    # Try to get activity time from tmux
    # Note: display-message and capture-pane don't support = prefix for exact match
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session, "#{pane_last_activity}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            last_activity = int(result.stdout.strip())
            current_time = int(time.time())
            idle_seconds = current_time - last_activity
            return idle_seconds >= idle_threshold
    except (subprocess.TimeoutExpired, ValueError, subprocess.SubprocessError):
        pass

    # Fallback: check if output is changing
    # This is less reliable but works as a backup
    return False


def wait_for_idle(
    session: str,
    timeout: float = 300.0,
    idle_threshold: float = 3.0,
    poll_interval: float = 2.0,
    stable_count: int = 3,
) -> bool:
    """Wait for a session to become idle.

    "Idle" means the session has not produced output for idle_threshold seconds
    for stable_count consecutive checks.

    Args:
        session: Session name
        timeout: Maximum seconds to wait
        idle_threshold: Seconds of inactivity to consider idle
        poll_interval: Seconds between checks
        stable_count: Number of consecutive idle checks required

    Returns:
        True if session became idle, False if timeout

    Raises:
        CompletionTimeout: If timeout is exceeded
    """
    start_time = time.time()
    consecutive_idle = 0
    last_output_hash = None

    while True:
        elapsed = time.time() - start_time
        if elapsed >= timeout:
            raise CompletionTimeout(
                f"Timeout waiting for session '{session}' to become idle "
                f"after {timeout:.0f}s"
            )

        # Check if session is still running
        result = subprocess.run(
            ["tmux", "has-session", "-t", f"={session}"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise CompletionError(f"Session '{session}' no longer exists")

        # Get current output snapshot
        # Note: capture-pane doesn't support = prefix for exact match
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", "-20"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            time.sleep(poll_interval)
            continue

        current_hash = hash(result.stdout)

        if current_hash == last_output_hash:
            consecutive_idle += 1
            if consecutive_idle >= stable_count:
                return True
        else:
            consecutive_idle = 0
            last_output_hash = current_hash

        time.sleep(poll_interval)


def wait_for_file(
    path: Path,
    timeout: float = 60.0,
    poll_interval: float = 1.0,
) -> bool:
    """Wait for a file to be created.

    Args:
        path: Path to the file
        timeout: Maximum seconds to wait
        poll_interval: Seconds between checks

    Returns:
        True if file was created

    Raises:
        CompletionTimeout: If timeout is exceeded
    """
    start_time = time.time()

    while True:
        if path.exists() and path.stat().st_size > 0:
            # Give a moment for the file to be fully written
            time.sleep(0.5)
            return True

        elapsed = time.time() - start_time
        if elapsed >= timeout:
            raise CompletionTimeout(
                f"Timeout waiting for file '{path}' after {timeout:.0f}s"
            )

        time.sleep(poll_interval)


def parse_summary_file(path: Path) -> SummaryResult:
    """Parse a task summary file.

    Supports two formats:

    1. YAML front matter (from Python SYSTEM_SUMMARY_PROMPT):
        ---
        status: complete
        summary: Did the thing
        files_modified:
          - path/to/file
        ---

    2. Markdown headings (from hook summary prompt):
        # Task Summary
        ## Status
        complete
        ## What Was Done
        Description here
        ## Notes
        Extra context

    Args:
        path: Path to the summary file

    Returns:
        SummaryResult with parsed fields

    Raises:
        CompletionError: If file cannot be parsed
    """
    try:
        content = path.read_text()
    except OSError as e:
        raise CompletionError(f"Cannot read summary file: {e}")

    # Default values
    status = "incomplete"
    summary = ""
    files_modified: list[str] = []
    blockers: list[str] = []

    if content.startswith("---"):
        # Parse YAML front matter format
        end_match = re.search(r"\n---\s*\n", content[3:])
        if end_match:
            yaml_content = content[3:3 + end_match.start()]

            # Track which list we're currently parsing
            current_list: str | None = None

            for line in yaml_content.split("\n"):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                if stripped.startswith("status:"):
                    status = stripped.split(":", 1)[1].strip()
                    current_list = None
                elif stripped.startswith("summary:"):
                    summary = stripped.split(":", 1)[1].strip()
                    current_list = None
                elif stripped.startswith("files_modified:"):
                    current_list = "files"
                elif stripped.startswith("blockers:"):
                    current_list = "blockers"
                elif stripped.startswith("- "):
                    item = stripped[2:].strip()
                    if current_list == "files":
                        files_modified.append(item)
                    elif current_list == "blockers":
                        blockers.append(item)
    else:
        # Parse markdown heading format (## Status, ## What Was Done, etc.)
        sections: dict[str, list[str]] = {}
        current_section: str | None = None

        for line in content.split("\n"):
            stripped = line.strip()
            heading = re.match(r"^#{1,3}\s+(.+)", stripped)
            if heading:
                current_section = heading.group(1).lower()
                continue
            if current_section and stripped:
                sections.setdefault(current_section, []).append(stripped)

        if "status" in sections:
            status = sections["status"][0].strip().lower()
        if "what was done" in sections:
            summary = " ".join(sections["what was done"])
        elif "summary" in sections:
            summary = " ".join(sections["summary"])

    # Validate status — also accept "error" as "failed"
    if status == "error":
        status = "failed"
    if status not in ("complete", "incomplete", "failed"):
        status = "incomplete"

    return SummaryResult(
        status=status,
        summary=summary,
        files_modified=files_modified,
        blockers=blockers,
        raw_content=content,
    )


def status_to_exit_code(status: str) -> int:
    """Convert status string to exit code.

    Args:
        status: Task status (complete, incomplete, failed, usage_limit)

    Returns:
        Exit code (0=complete, 1=failed, 2=incomplete, 7=usage_limit)
    """
    if status == "complete":
        return 0
    elif status == "failed":
        return 1
    elif status == "usage_limit":
        return 7
    else:
        return 2


# =============================================================================
# Loop mode helpers
# =============================================================================

# Directory for iteration review files (relative to project root)
ITERATIONS_DIR = ".agentwire/iterations"


def generate_iteration_filename(session: str, iteration: int) -> str:
    """Generate a filename for an iteration review file.

    Args:
        session: tmux session name
        iteration: Current iteration number (1-based)

    Returns:
        Relative path like .agentwire/iterations/mysession-iter-1.md
    """
    return f"{ITERATIONS_DIR}/{session}-iter-{iteration}.md"


# Prompt sent between loop iterations to get a review
ITERATION_REVIEW_PROMPT = """Review your progress so far. Write a brief status report to {iter_file}:

# Iteration {iteration} Review

## Status
complete | incomplete

## What Was Done
[Brief description of work in this iteration]

## Remaining Work
[What still needs to be done, or "none" if complete]

Use "complete" if the task is fully done. Use "incomplete" if more work is needed.
Write the file now."""


# Prompt sent to continue the loop with context
ITERATION_CONTINUE_PROMPT = """Continue working on the task. This is iteration {iteration} of {max_iterations}.

Previous iteration reviews are in {iterations_dir}/ — read them for context on what's been done.

Original task:
{original_prompt}

Continue where you left off. Focus on remaining work identified in previous reviews."""
