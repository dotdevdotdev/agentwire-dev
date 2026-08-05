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


class CompletionTimeout(CompletionError):  # noqa: N818  # public API name, renaming breaks callers
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


def clear_task_context(session: str) -> None:
    """Remove task context and completion signal files.

    Args:
        session: tmux session name
    """
    context_file = TASKS_DIR / f"{session}.json"
    try:
        context_file.unlink(missing_ok=True)
    except OSError:
        pass


def wait_for_completion_signal(
    session: str,
    poll_interval: float = 10.0,
    summary_path: Path | None = None,
    max_duration: int = 0,
    transcript_since: float | None = None,
) -> dict:
    """Wait for task completion by polling the summary file directly.

    Exits when:
    1. Summary file appears (task completed normally)
    2. Session dies (agent crashed, tmux killed)
    3. ``max_duration`` elapses, when the task sets one
    4. The session is parked on a usage limit (``status=usage_limit``)
    5. Claude refuses the turn for an expired login (``status=auth_expired``,
       #906) — the one exit that is provably terminal rather than slow, since
       no completion signal can arrive until a human runs ``/login``

    Completion is otherwise agent-driven: the idle hook fires, the agent writes
    a summary, and this returns. An agent that never goes idle never produces
    either exit — wedged on an unrecognized dialog, or blocked inside a tool
    call — and before ``max_duration`` existed the wait was unbounded, so that
    read as a silent multi-hour hang whose only visible end was the scheduler's
    4h process-group watchdog (#867).

    ``max_duration`` is a per-attempt wall clock, not an idle timer: it can't
    tell a wedged agent from a slow one, so a task that legitimately runs long
    should set it high or leave it at 0 rather than get killed mid-work.

    Args:
        session: tmux session name
        poll_interval: Seconds between checks
        summary_path: Path to the summary .md file the agent will write
        max_duration: Seconds before giving up (0 = unbounded)
        transcript_since: Epoch floor for "was this transcript written by the
            current attempt?" (#906). Callers pass the moment the ATTEMPT
            began — before the session launch and the prompt send — because
            the refusal is recorded ~15ms after the prompt submits, which is
            still seconds before this wait is entered. Defaults to the wait's
            own start, which is correct only for callers that had no earlier
            anchor and is deliberately the conservative direction: too-late a
            floor misses a detection, it never invents one.

    Returns:
        Dict with 'status', 'summary', 'summary_file' keys

    Raises:
        CompletionTimeout: If the session dies, or max_duration elapses, before
            the task completed. The message names which.
    """
    # Build glob pattern for fuzzy summary detection (agents sometimes
    # invent their own timestamp instead of using the provided filename).
    summary_glob = None
    if summary_path:
        # e.g. task-summary-scheduler-daily-book-report-daily-report-*.md
        stem = summary_path.stem  # without .md
        # Strip the timestamp suffix (last 19 chars: YYYY-MM-DDTHH-MM-SS)
        prefix = stem[:-19] if len(stem) > 19 else stem
        summary_glob = f"{prefix}*.md"

    started = time.time()

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
                # A written summary is proof a turn actually ran, which is
                # proof the login works — so clear any recorded outage here
                # rather than making the fleet wait out OUTAGE_TTL (#906).
                # This is the hook that makes "reopens on the first successful
                # turn" true; without it that promise was operator-facing text
                # describing behavior the code did not have, which is the very
                # defect #906 exists to fix, one scale down.
                from .auth_expired import clear_state

                clear_state()
                return {
                    "status": result.status,
                    "summary": result.summary,
                    "summary_file": str(found_summary),
                }
            except CompletionError:
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

        # Expired Claude login: the turn was REFUSED, so no completion signal
        # can ever arrive (#906). This is the state that made #867 cost two
        # hours — the pane is alive, the agent process is running, and the
        # usage-limit check above sees no dialog, so every liveness test below
        # passes forever. Detected from the transcript, which records the
        # refusal as a structured `error: authentication_failed`. Checked
        # BEFORE `_session_has_agent` so the run reports the cause rather than
        # the eventual symptom.
        from .auth_expired import check_and_flag, summary_line

        detail = check_and_flag(
            session,
            project_path=summary_path.parent if summary_path else None,
            since=started if transcript_since is None else transcript_since,
            source="ensure",
        )
        if detail is not None:
            return {"status": "auth_expired", "summary": summary_line(detail)}

        # Session gone or agent crashed (fell back to bare shell)
        if not _session_has_agent(session):
            raise CompletionTimeout(
                f"Session '{session}' died or agent exited before task completed"
            )

        if max_duration > 0:
            elapsed = time.time() - started
            if elapsed >= max_duration:
                raise CompletionTimeout(
                    f"Task exceeded max_duration ({max_duration}s) after "
                    f"{int(elapsed)}s — the agent never signalled completion"
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
        status: Task status (complete, incomplete, failed, usage_limit,
            auth_expired)

    Returns:
        Exit code (0=complete, 1=failed, 2=incomplete, 7=usage_limit,
        8=auth_expired)
    """
    if status == "complete":
        return 0
    elif status == "failed":
        return 1
    elif status == "usage_limit":
        return 7
    elif status == "auth_expired":
        # Distinct from incomplete so the scheduler can gate the rest of the
        # fleet instead of letting each task discover the outage itself (#906).
        return 8
    else:
        return 2
