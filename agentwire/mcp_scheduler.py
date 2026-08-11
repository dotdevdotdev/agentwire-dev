"""MCP tools — scheduler domain."""

from .core import run_agentwire_cmd
from .mcp_core import (
    mcp,
)


@mcp.tool()
def scheduler_status() -> str:
    """Check scheduler daemon health and next task due.

    Returns:
        Scheduler status including running state, task counts, and next task.
    """
    data = run_agentwire_cmd(["scheduler", "status"])
    if not data.get("success"):
        return f"Failed to get scheduler status: {data.get('error', 'Unknown error')}"

    running = "running" if data.get("running") else "stopped"
    task_count = data.get("task_count", 0)
    enabled = data.get("enabled_count", 0)
    next_task = data.get("next_task")
    next_in = data.get("next_in_seconds", 0)

    lines = [f"Scheduler: {running}"]
    lines.append(f"Tasks: {enabled}/{task_count} enabled")

    if next_task:
        if next_in <= 0:
            lines.append(f"Next: {next_task} (due now)")
        else:
            mins = int(next_in) // 60
            secs = int(next_in) % 60
            lines.append(f"Next: {next_task} (in {mins}m {secs}s)")
    else:
        lines.append("Next: nothing due")

    return "\n".join(lines)


@mcp.tool()
def scheduler_board() -> str:
    """Show scheduler task board with overdue scores.

    Returns:
        Full board with task names, intervals, last run times, and overdue scores.
    """
    data = run_agentwire_cmd(["scheduler", "board"])
    if not data.get("success"):
        return f"Failed to get board: {data.get('error', 'Unknown error')}"

    tasks = data.get("tasks", [])
    if not tasks:
        return "No tasks in scheduler board."

    lines = ["Scheduler board:"]
    for t in tasks:
        label = t.get("label", t.get("name", "unknown"))
        if not t.get("enabled"):
            label = f"{label} [disabled]"
        status = t.get("last_status", "never")
        overdue = t.get("overdue_str", "?")
        schedule = t.get("schedule_str", "?")
        last_run = t.get("last_run", "never")
        lines.append(f"  - {label}: {status}, schedule {schedule}, last run {last_run}, overdue {overdue}")

    return "\n".join(lines)


@mcp.tool()
def scheduler_live() -> str:
    """Show live scheduler state including current task, uptime, and counters.

    Returns:
        Live scheduler state or error if scheduler is not running.
    """
    data = run_agentwire_cmd(["scheduler", "live", "--json"])
    if not data.get("success"):
        return f"Scheduler not running or no live state: {data.get('error', 'Unknown error')}"

    status = data.get("status", "unknown")
    uptime = data.get("uptime_seconds", 0)
    current = data.get("current_task")
    completed = data.get("tasks_completed", 0)
    failed = data.get("tasks_failed", 0)
    next_task = data.get("next_task")
    next_in = data.get("next_in_seconds", 0)

    # Format uptime
    hours = uptime // 3600
    mins = (uptime % 3600) // 60
    uptime_str = f"{hours}h{mins}m" if hours else f"{mins}m"

    lines = [f"Scheduler: {status} (uptime {uptime_str})"]
    if current:
        lines.append(f"Current: {current}")
    else:
        lines.append("Current: idle")
    lines.append(f"Completed: {completed} | Failed: {failed}")
    if next_task:
        next_mins = int(next_in) // 60
        next_secs = int(next_in) % 60
        lines.append(f"Next: {next_task} (in {next_mins}m {next_secs}s)")

    return "\n".join(lines)


@mcp.tool()
def scheduler_events(tail: int = 20, task: str = "") -> str:
    """Show recent scheduler events from the event log.

    Args:
        tail: Number of recent events to show (default: 20)
        task: Filter events by task name (optional)

    Returns:
        Recent scheduler events formatted for reading.
    """
    args = ["scheduler", "events", "--json", "--tail", str(tail)]
    if task:
        args.extend(["--task", task])

    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to get events: {data.get('error', 'Unknown error')}"

    events = data.get("events", [])
    if not events:
        return "No scheduler events."

    lines = ["Recent scheduler events:"]
    for evt in events:
        ts = evt.get("ts", "")
        # Trim to just time portion
        ts_short = ts[11:16] if len(ts) > 16 else ts
        etype = evt.get("event", "?")
        task_name = evt.get("task", "")

        if etype == "task_completed":
            status = evt.get("status", "?")
            duration = evt.get("duration", 0)
            summary = evt.get("summary", "")
            detail = f"{status} {duration}s"
            if summary:
                detail += f' — "{summary}"'
            lines.append(f"  {ts_short} {etype}: {task_name} ({detail})")
        elif etype == "task_started":
            session = evt.get("session", "")
            lines.append(f"  {ts_short} {etype}: {task_name} → {session}")
        elif etype == "task_skipped":
            reason = evt.get("reason", "?")
            lines.append(f"  {ts_short} {etype}: {task_name} ({reason})")
        else:
            lines.append(f"  {ts_short} {etype}: {task_name}")

    return "\n".join(lines)


@mcp.tool()
def scheduler_run(task: str) -> str:
    """Force-run a scheduler task immediately.

    Dispatches the task via `agentwire ensure` and updates the board state.

    Args:
        task: Task name from the scheduler board.

    Returns:
        Task result with status and duration.
    """
    data = run_agentwire_cmd(["scheduler", "run", task], timeout=600)
    if not data.get("success"):
        return f"Failed to run task: {data.get('error', 'Unknown error')}"

    status = data.get("status", "unknown")
    duration = data.get("duration", 0)
    return f"Task '{task}' completed: {status} ({duration}s)"


@mcp.tool()
def scheduler_enable(task: str) -> str:
    """Enable a disabled task in the scheduler board.

    Args:
        task: Task name to enable.

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["scheduler", "enable", task], json_output=False)
    if data.get("success"):
        return f"Task '{task}' enabled."
    return f"Failed to enable task: {data.get('error', 'Unknown error')}"


@mcp.tool()
def scheduler_disable(task: str) -> str:
    """Disable a task in the scheduler board.

    Disabled tasks are skipped during scheduling.

    Args:
        task: Task name to disable.

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["scheduler", "disable", task], json_output=False)
    if data.get("success"):
        return f"Task '{task}' disabled."
    return f"Failed to disable task: {data.get('error', 'Unknown error')}"


@mcp.tool()
def scheduler_history(limit: int = 20) -> str:
    """Show recent run history from board state.

    Args:
        limit: Maximum number of results (default: 20)

    Returns:
        Formatted run history with task names, last run times, and statuses.
    """
    data = run_agentwire_cmd(["scheduler", "history", "--json"])
    if not data.get("success"):
        return f"Failed to get history: {data.get('error', 'Unknown error')}"

    history = data.get("history", [])
    if not history:
        return "No run history."

    # Sort by last_run descending, limit results
    history.sort(key=lambda h: h.get("last_run") or "", reverse=True)
    history = history[:limit]

    lines = ["Recent scheduler history:"]
    for entry in history:
        task_name = entry.get("task", "?")
        last_run = entry.get("last_run", "never")
        if last_run and len(last_run) > 16:
            last_run = last_run[:16].replace("T", " ")
        status = entry.get("last_status", "?")
        duration = entry.get("last_duration")
        runs = entry.get("run_count", 0)
        dur_str = f"{duration}s" if duration else "-"
        lines.append(f"  {task_name}: {last_run} — {status} ({dur_str}, {runs} runs)")

    return "\n".join(lines)


@mcp.tool()
def scheduler_report(since: str = "8h", artifact: bool = False) -> str:
    """Generate a morning report of recent task runs.

    Produces an HTML artifact summarizing all tasks that ran in the time window,
    with statuses, durations, branches, and PR links.

    Args:
        since: Time window to cover (e.g. '8h', '12h', '1d') default: '8h'
        artifact: If True, announce the report as a click-to-open portal notification

    Returns:
        Path to generated HTML report and summary statistics.
    """
    cmd = ["scheduler", "report", "--since", since, "--json"]
    if artifact:
        cmd.append("--artifact")
    data = run_agentwire_cmd(cmd)
    if not data.get("success"):
        return f"Failed to generate report: {data.get('error', 'Unknown error')}"

    path = data.get("path", "")
    total = data.get("total", 0)
    complete = data.get("complete", 0)
    failed = data.get("failed", 0)
    incomplete = data.get("incomplete", 0)
    return (
        f"Morning report generated: {path}\n"
        f"Tasks: {total} total — {complete} complete, {failed} failed, {incomplete} incomplete"
    )
