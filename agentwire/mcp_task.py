"""MCP tools — task domain."""

from .mcp_core import (
    mcp,
    run_agentwire_cmd,
)


@mcp.tool()
def task_list(session: str | None = None) -> str:
    """List available tasks for a session/project.

    Args:
        session: Session name (uses its project's .agentwire.yml)

    Returns:
        List of tasks with their configurations.
    """
    args = ["task", "list"]
    if session:
        args.append(session)

    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to list tasks: {data.get('error', 'Unknown error')}"

    tasks = data.get("tasks", [])
    if not tasks:
        return "No tasks defined in .agentwire.yml"

    lines = ["Available tasks:"]
    for t in tasks:
        name = t.get("name", "unknown")
        has_pre = "with pre-commands" if t.get("has_pre") else ""
        retries = f"retries={t.get('retries', 0)}" if t.get("retries", 0) > 0 else ""
        extras = ", ".join(filter(None, [has_pre, retries]))
        lines.append(f"  - {name}" + (f" ({extras})" if extras else ""))

    return "\n".join(lines)


@mcp.tool()
def task_show(session: str, task: str) -> str:
    """Show task definition details.

    Args:
        session: Session name
        task: Task name from .agentwire.yml

    Returns:
        Task configuration details.
    """
    args = ["task", "show", f"{session}/{task}"]
    data = run_agentwire_cmd(args)

    if not data.get("success"):
        return f"Failed to show task: {data.get('error', 'Unknown error')}"

    lines = [f"Task: {data.get('name', task)}"]
    lines.append(f"  Shell: {data.get('shell') or '/bin/sh'}")
    lines.append(f"  Retries: {data.get('retries', 0)}")
    lines.append(f"  Idle timeout: {data.get('idle_timeout', 30)}s")

    if pre := data.get("pre"):
        lines.append(f"  Pre-commands: {len(pre)}")

    if data.get("on_task_end"):
        lines.append("  Has on_task_end prompt")

    if post := data.get("post"):
        lines.append(f"  Post-commands: {len(post)}")

    if issues := data.get("validation_issues"):
        lines.append(f"  Validation issues: {', '.join(issues)}")

    return "\n".join(lines)


@mcp.tool()
def task_run(session: str, task: str, timeout: int = 300) -> str:
    """Run a named task with full lifecycle.

    Executes full task lifecycle:
    1. Acquire lock, ensure session exists and is healthy
    2. Run pre-commands, validate outputs
    3. Send templated prompt, wait for idle
    4. Send system summary prompt, wait for summary file
    5. Send on_task_end if defined, wait for idle
    6. Run post-commands
    7. Release lock

    Completes when the agent writes a summary file or the session dies.
    The timeout parameter controls how long the MCP call waits (not the task).

    Args:
        session: Target session name
        task: Task name from .agentwire.yml
        timeout: Max seconds for MCP call to wait (default 300)

    Returns:
        Task result with status, summary, and attempt count.
    """
    args = ["ensure", "-s", session, "--task", task]

    # MCP call timeout — ensure itself has no timeout, it exits on session death
    data = run_agentwire_cmd(args, timeout=timeout + 60)

    if not data.get("success"):
        error = data.get("error", "Unknown error")
        exit_code = data.get("exit_code")

        # Provide context based on exit code
        if exit_code == 3:
            return f"Task failed: Session is locked by another process. {error}"
        elif exit_code == 4:
            return f"Task failed: Pre-command error. {error}"
        elif exit_code == 5:
            return f"Task failed: Timeout after {timeout}s. {error}"
        elif exit_code == 6:
            return f"Task failed: Session error. {error}"
        else:
            return f"Task failed: {error}"

    status = data.get("status", "unknown")
    summary = data.get("summary", "")
    attempt = data.get("attempt", 1)
    summary_file = data.get("summary_file", "")

    lines = [f"Task {task} completed with status: {status}"]
    if summary:
        lines.append(f"Summary: {summary}")
    if attempt > 1:
        lines.append(f"Completed on attempt {attempt}")
    if summary_file:
        lines.append(f"Summary file: {summary_file}")

    return "\n".join(lines)


@mcp.tool()
def task_validate(session: str, task: str) -> str:
    """Validate a task configuration for errors.

    Args:
        session: Session name
        task: Task name from .agentwire.yml

    Returns:
        Validation results with any issues found.
    """
    data = run_agentwire_cmd(["task", "validate", f"{session}/{task}"])
    if not data.get("success"):
        return f"Failed to validate task: {data.get('error', 'Unknown error')}"

    issues = data.get("issues", [])
    if not issues:
        return f"Task '{task}' is valid."

    lines = [f"Task '{task}' has {len(issues)} issue(s):"]
    for issue in issues:
        lines.append(f"  - {issue}")

    return "\n".join(lines)
