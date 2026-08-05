"""CLI for the task scheduler — ``agentwire scheduler ...``.

The scheduler is a centralized daemon (``agentwire scheduler serve``, run in a
tmux session) that dispatches ensure/workflow tasks across projects on a shared
cadence. These commands are the human-facing surface: start/stop the daemon,
inspect the board and live state, force-run or toggle tasks, and render the
morning report / live dashboard artifacts. Per the CLAUDE.md SSOT rule the
logic lives here; the portal REST endpoints are thin wrappers.
"""

from __future__ import annotations

import datetime
import subprocess
import sys
import time
from pathlib import Path

from .core import (
    _check_tmux_installed,
    _output_json,
    _output_result,
    tmux_session_exists,
)
from .scheduler.models import SCHEDULER_SESSION

# =============================================================================
# Scheduler Commands
# =============================================================================


def _describe_live_daemon(state: dict) -> str:
    """One-line identification of an already-running daemon, for refusals."""
    pid = state.get("pid")
    started = state.get("started_at") or "unknown start time"
    return f"pid {pid}, up since {started}"


def cmd_scheduler_start(args) -> int:
    """Start the scheduler daemon in a tmux session."""
    from .scheduler import live_daemon_state

    if not _check_tmux_installed():
        return 1

    # A daemon supervised outside tmux (launchd) has no session to find, so the
    # tmux check below would happily start a SECOND dispatcher onto the same
    # board (#873). Ask the live-state file first — it knows either way.
    live = live_daemon_state()
    if live and not tmux_session_exists(SCHEDULER_SESSION):
        print(f"Scheduler already running outside tmux ({_describe_live_daemon(live)}).")
        print("Refusing to start a second dispatcher on the same board.")
        return 1

    if tmux_session_exists(SCHEDULER_SESSION):
        print(f"Scheduler already running in tmux session '{SCHEDULER_SESSION}'")
        print("Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", SCHEDULER_SESSION])
        return 0

    print(f"Starting scheduler daemon in tmux session '{SCHEDULER_SESSION}'...")
    subprocess.run([
        "tmux", "new-session", "-d", "-s", SCHEDULER_SESSION,
    ])
    subprocess.run([
        "tmux", "send-keys", "-t", SCHEDULER_SESSION,
        "agentwire scheduler serve", "Enter",
    ])

    print("Attaching... (Ctrl+B D to detach)")
    subprocess.run(["tmux", "attach-session", "-t", SCHEDULER_SESSION])
    return 0


def cmd_scheduler_serve(args) -> int:
    """Run the scheduler loop in the foreground (for tmux or an external supervisor).

    Refuses to become a second dispatcher (#873). Two daemons on one board
    double-dispatch tasks, and the only thing that used to stop it was the tmux
    session-name collision in ``scheduler start`` — an accident of naming that
    doesn't fire at all when the other daemon is supervised elsewhere.
    """
    from .scheduler import live_daemon_state, run_scheduler_loop

    live = live_daemon_state()
    if live and not getattr(args, "force", False):
        print(f"A scheduler daemon is already running ({_describe_live_daemon(live)}).",
              file=sys.stderr)
        print("Refusing to start a second dispatcher on the same board — "
              "stop the running one first, or pass --force if it is a stale record.",
              file=sys.stderr)
        return 1

    run_scheduler_loop()
    return 0


def cmd_scheduler_stop(args) -> int:
    """Stop the scheduler daemon."""
    from .scheduler import live_daemon_state

    if not tmux_session_exists(SCHEDULER_SESSION):
        live = live_daemon_state()
        if live:
            # Running, just not in tmux — say so rather than the flatly false
            # "not running" the tmux-only check used to print (#873).
            print(f"Scheduler is running outside tmux ({_describe_live_daemon(live)}) "
                  "— stop it through its supervisor (e.g. launchctl).")
            return 1
        print("Scheduler is not running.")
        return 1

    subprocess.run(["tmux", "kill-session", "-t", SCHEDULER_SESSION])
    print("Scheduler stopped.")
    return 0


def cmd_scheduler_status(args) -> int:
    """Show scheduler status and next task due."""
    from .config import get_config
    from .scheduler import (
        format_interval,
        live_daemon_state,
        load_board,
        pick_next_task,
        read_events,
    )

    json_mode = getattr(args, 'json', False)
    # Liveness comes from the daemon's own live-state record, not from tmux
    # (#873): a launchd-supervised daemon has no tmux session and used to print
    # "stopped" while it was dispatching.
    live = live_daemon_state()
    running = live is not None
    board_path = get_config().scheduler.board_file

    if not board_path.exists():
        return _output_result(
            False, json_mode,
            f"Board file not found: {board_path}",
            running=running,
        )

    try:
        board = load_board()
    except (FileNotFoundError, ValueError) as e:
        return _output_result(False, json_mode, str(e), running=running)

    task_count = len(board.tasks)
    enabled_count = sum(1 for t in board.tasks.values() if t.enabled)
    next_task, wait_seconds = pick_next_task(board)
    recent_activity = _recent_activity(read_events(tail=60), limit=5)

    result = {
        "running": running,
        "pid": live.get("pid") if live else None,
        "in_tmux": tmux_session_exists(SCHEDULER_SESSION),
        "board_path": str(board_path),
        "task_count": task_count,
        "enabled_count": enabled_count,
        "next_task": next_task,
        "next_in_seconds": round(wait_seconds, 1),
        "recent_activity": recent_activity,
    }

    if json_mode:
        _output_json({"success": True, **result})
        return 0

    if running:
        where = "tmux" if result["in_tmux"] else "external supervisor"
        status_str = f"running (pid {live.get('pid')}, {where})"
    else:
        status_str = "stopped"
    print(f"Scheduler: {status_str}")
    print(f"Board: {board_path}")
    print(f"Tasks: {enabled_count}/{task_count} enabled")

    if next_task:
        if wait_seconds <= 0:
            print(f"Next: {next_task} (due now)")
        else:
            print(f"Next: {next_task} (in {format_interval(int(wait_seconds))})")
    else:
        print("Next: nothing due")

    if recent_activity:
        print("\nRecent activity:")
        for item in recent_activity:
            print(f"  {item['when']:<16} {item['task']:<24} {item['detail']}")

    return 0


def _recent_activity(events: list[dict], limit: int = 5) -> list[dict]:
    """Distill the event stream into a short 'what just happened' list.

    Keeps the outcome-bearing events (completed/failed/gate-error) so a
    glance at `scheduler status` shows recent results — including the
    fail-open gate errors that used to vanish entirely.
    """
    keep = {"task_completed", "task_failed", "gate_error", "task_gated"}
    out: list[dict] = []
    for evt in reversed(events):
        etype = evt.get("event")
        if etype not in keep:
            continue
        ts = evt.get("ts", "")
        try:
            when = datetime.datetime.fromisoformat(ts).strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            when = ts[:16] if ts else "?"
        if etype == "task_completed":
            status = evt.get("status", "?")
            summary = evt.get("summary", "")
            detail = f"{status}" + (f" — {summary}" if summary else "")
        elif etype == "task_failed":
            detail = "failed — " + (evt.get("summary") or evt.get("reason") or "?")
        elif etype == "task_gated":
            detail = f"[gated] {evt.get('gate_type', '?')}: {evt.get('reason', '?')}"
        else:  # gate_error
            detail = f"[gate-error] {evt.get('gate_type', '?')}: {evt.get('reason', '?')}"
        if len(detail) > 80:
            detail = detail[:79] + "…"
        out.append({"when": when, "task": evt.get("task", "?"), "detail": detail})
        if len(out) >= limit:
            break
    return out


# Statuses whose last_summary is worth surfacing as a "why" line on the board.
_BAD_STATUSES = {"failed", "incomplete", "timeout", "lock_conflict", "usage_limit"}


def cmd_scheduler_board(args) -> int:
    """Show full task board with overdue scores."""
    from .scheduler import get_board_display, load_board

    json_mode = getattr(args, 'json', False)

    try:
        board = load_board()
    except (FileNotFoundError, ValueError) as e:
        return _output_result(False, json_mode, str(e))

    rows = get_board_display(board)

    if json_mode:
        _output_json({"success": True, "tasks": rows})
        return 0

    if not rows:
        print("No tasks in board.")
        return 0

    # Group by project
    groups: dict[str, list[dict]] = {}
    for r in rows:
        proj = r["project"].rstrip("/").split("/")[-1]
        groups.setdefault(proj, [])
        groups[proj].append(r)

    # Summary line
    total = len(rows)
    regular = sum(1 for r in rows if not r["filler"])
    fillers = total - regular
    enabled = sum(1 for r in rows if r["enabled"])
    print(f"Scheduler board: {total} tasks ({regular} regular + {fillers} filler), {enabled} enabled\n")

    for proj, items in groups.items():
        # Sort: regular first (by priority), then filler (by priority)
        reg = sorted([r for r in items if not r["filler"]], key=lambda r: r["priority"])
        fil = sorted([r for r in items if r["filler"]], key=lambda r: r["priority"])

        session = items[0]["session"]
        print(f"  {proj} ({len(items)} tasks) → {session}")
        print(f"  {'Task':<30} {'Type':<16} {'Schedule':<24} {'Last Run':<16} {'Status':<12} {'Overdue'}")
        print(f"  {'-' * 114}")

        for r in reg + fil:
            task_name = r["task"]
            if not r["enabled"]:
                task_name = f"{task_name} [off]"

            if r["filler"]:
                type_str = f"filler (p{r['priority']})"
            elif r["priority"] != 99:
                type_str = f"regular (p{r['priority']})"
            else:
                type_str = "regular"

            status_str = r["last_status"]
            if r.get("in_flight"):
                status_str = "[in-flight]"

            schedule_display = r.get("schedule_str", "?")
            if len(schedule_display) > 22:
                schedule_display = schedule_display[:21] + "…"

            print(
                f"  {task_name:<30} "
                f"{type_str:<16} "
                f"{schedule_display:<24} "
                f"{r['last_run']:<16} "
                f"{status_str:<12} "
                f"{r['overdue_str']}"
            )

            # Surface WHY: a gate-eval error (fail-open, would otherwise be
            # invisible) takes precedence; then a currently-blocking gate
            # (so "+4h11m" reads as "waiting on gate", not silently falling
            # behind, #803); else the summary behind a bad status.
            detail = ""
            if r.get("last_gate_error"):
                detail = f"[gate-error] {r['last_gate_error']}"
            elif r.get("last_gate_skip"):
                detail = f"[gated] {r['last_gate_skip']}"
            elif status_str in _BAD_STATUSES and r.get("last_summary"):
                detail = r["last_summary"]
            if detail:
                if len(detail) > 96:
                    detail = detail[:95] + "…"
                print(f"  {'':<30} ↳ {detail}")

        print()

    return 0


def cmd_scheduler_run(args) -> int:
    """Force-run a specific task now."""
    from .scheduler import dispatch_task, load_board, save_board

    json_mode = getattr(args, 'json', False)
    name = args.name

    try:
        board = load_board()
    except (FileNotFoundError, ValueError) as e:
        return _output_result(False, json_mode, str(e))

    if name not in board.tasks:
        return _output_result(
            False, json_mode,
            f"Task '{name}' not found in board. Available: {', '.join(board.tasks.keys())}",
        )

    if not json_mode:
        print(f"Running: {name}")

    state = dispatch_task(board, name)
    board.state[name] = state
    save_board(board)

    if json_mode:
        _output_json({
            "success": state.last_status == "complete",
            "task": name,
            "status": state.last_status,
            "duration": state.last_duration,
            "run_count": state.run_count,
        })
        return 0 if state.last_status == "complete" else 1

    print(f"Done: {name} → {state.last_status} ({state.last_duration}s)")
    return 0 if state.last_status == "complete" else 1


def cmd_scheduler_enable(args) -> int:
    """Enable a task in the board."""
    return _set_task_enabled(args.name, True)


def cmd_scheduler_disable(args) -> int:
    """Disable a task in the board."""
    return _set_task_enabled(args.name, False)


def _set_task_enabled(name: str, enabled: bool) -> int:
    """Toggle a task's enabled field in the board YAML."""
    import yaml

    from .config import get_config

    board_path = get_config().scheduler.board_file

    if not board_path.exists():
        print(f"Board file not found: {board_path}", file=sys.stderr)
        return 1

    with open(board_path) as f:
        raw = yaml.safe_load(f) or {}

    tasks = raw.get("tasks", {})
    if name not in tasks:
        print(f"Task '{name}' not found in board.", file=sys.stderr)
        return 1

    tasks[name]["enabled"] = enabled

    # Atomic + validated write — never leave scheduler.yaml half-written (#449).
    from .scheduler import _atomic_write

    text = yaml.dump(raw, default_flow_style=False, sort_keys=False)

    def _validate(tmp_path: str) -> None:
        with open(tmp_path) as f:
            reparsed = yaml.safe_load(f)
        if not isinstance(reparsed, dict) or "tasks" not in reparsed:
            raise ValueError("scheduler board failed re-parse validation")

    _atomic_write(board_path, text, validate=_validate)

    action = "Enabled" if enabled else "Disabled"
    print(f"{action}: {name}")
    return 0


def cmd_scheduler_history(args) -> int:
    """Show recent run history from board state."""
    from .scheduler import format_interval, load_board

    json_mode = getattr(args, 'json', False)

    try:
        board = load_board()
    except (FileNotFoundError, ValueError) as e:
        return _output_result(False, json_mode, str(e))

    if json_mode:
        history = []
        for name, state in board.state.items():
            history.append({
                "task": name,
                "last_run": state.last_run.isoformat() if state.last_run else None,
                "last_status": state.last_status,
                "last_duration": state.last_duration,
                "run_count": state.run_count,
            })
        _output_json({"success": True, "history": history})
        return 0

    if not board.state:
        print("No run history.")
        return 0

    print(f"{'Task':<30} {'Last Run':<20} {'Status':<14} {'Duration':<10} {'Runs'}")
    print("-" * 85)

    for name, state in sorted(board.state.items()):
        if state.last_run:
            lr = state.last_run.strftime("%Y-%m-%d %H:%M")
        else:
            lr = "never"

        dur = format_interval(state.last_duration) if state.last_duration else "-"
        print(f"{name:<30} {lr:<20} {state.last_status:<14} {dur:<10} {state.run_count}")

    return 0


def cmd_scheduler_report(args) -> int:
    """Generate a morning report HTML artifact of recent task runs."""

    from .scheduler import _parse_duration, format_interval, load_board, read_events

    json_mode = getattr(args, 'json', False)
    since_str = getattr(args, 'since', '8h') or '8h'
    open_artifact = getattr(args, 'artifact', False)

    # Parse duration
    since_seconds = _parse_duration(since_str) or 28800  # default 8h
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=since_seconds)

    # Load board state (validate it loads; events are read below)
    try:
        load_board()
    except Exception as e:
        print(f"Error loading board: {e}", file=sys.stderr)
        return 1

    # Load events in the window
    try:
        events = read_events(tail=500)
    except Exception:
        events = []

    # Collect completed task events within window
    runs: list[dict] = []
    for ev in events:
        if ev.get("event") != "task_completed":
            continue
        ts_str = ev.get("ts") or ev.get("timestamp", "")
        try:
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if ts < cutoff:
            continue
        task_name = ev.get("task", "")
        # Collect run data
        run = {
            "task": task_name,
            "status": ev.get("status", "unknown"),
            "duration": ev.get("duration", 0),
            "summary": ev.get("summary", ""),
            "timestamp": ts.strftime("%Y-%m-%d %H:%M"),
            "work_branch": "",
            "pr_url": "",
            "workflow": ev.get("workflow", ""),
            "run_id": ev.get("run_id", ""),
            "nodes": ev.get("nodes") or [],
        }
        runs.append(run)

    # Count totals
    total = len(runs)
    complete = sum(1 for r in runs if r["status"] == "complete")
    failed = sum(1 for r in runs if r["status"] in ("failed", "timeout"))
    incomplete = total - complete - failed

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    report_date = datetime.datetime.now().strftime("%Y-%m-%d")

    def status_badge(status: str) -> str:
        colors = {
            "complete": "#00c853",
            "failed": "#ff5252",
            "timeout": "#ff7043",
            "incomplete": "#ffa726",
            "unknown": "#78909c",
        }
        color = colors.get(status, "#78909c")
        return f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:12px;font-size:0.85em">{status}</span>'

    rows_html = ""
    for r in runs:
        duration_str = format_interval(r["duration"]) if r["duration"] else "-"
        pr_link = f'<a href="{r["pr_url"]}" target="_blank" style="color:#00d4ff">{r["pr_url"][:40]}...</a>' if r.get("pr_url") else "-"
        branch_col = f'<code style="font-size:0.85em">{r.get("work_branch") or "-"}</code>'
        summary_text = r["summary"][:120] if r["summary"] else "-"
        rows_html += f"""
        <tr>
          <td style="font-weight:600">{r["task"]}</td>
          <td>{status_badge(r["status"])}</td>
          <td>{r["timestamp"]}</td>
          <td>{duration_str}</td>
          <td>{branch_col}</td>
          <td>{pr_link}</td>
          <td style="color:#aaa;font-size:0.85em">{summary_text}</td>
        </tr>"""

    if not rows_html:
        rows_html = f'<tr><td colspan="7" style="color:#556;text-align:center;padding:24px">No tasks ran in the last {since_str}</td></tr>'

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Morning Report — {report_date}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; background: #1a1a2e; color: #e0e0e0; }}
  h1 {{ color: #00d4ff; margin-bottom: 4px; }}
  .meta {{ color: #556; font-size: 0.85em; margin-bottom: 20px; }}
  .summary-bar {{ display: flex; gap: 24px; padding: 14px 20px; background: #16213e; border-radius: 8px; margin-bottom: 24px; }}
  .summary-bar .item {{ display: flex; flex-direction: column; }}
  .summary-bar .label {{ color: #556; font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.5px; }}
  .summary-bar .value {{ font-size: 1.4em; font-weight: 700; }}
  .complete {{ color: #00c853; }}
  .failed {{ color: #ff5252; }}
  .incomplete {{ color: #ffa726; }}
  .total {{ color: #e0e0e0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #2a2a4a; font-size: 0.9em; }}
  th {{ background: #16213e; color: #00d4ff; font-weight: 600; position: sticky; top: 0; }}
  tr:hover {{ background: #16213e; }}
  code {{ background: #0d1b2a; padding: 2px 6px; border-radius: 4px; }}
</style>
</head>
<body>
<h1>Morning Report</h1>
<p class="meta">Generated {now_str} &nbsp;&middot;&nbsp; Last {since_str}</p>

<div class="summary-bar">
  <div class="item"><span class="label">Total</span><span class="value total">{total}</span></div>
  <div class="item"><span class="label">Complete</span><span class="value complete">{complete}</span></div>
  <div class="item"><span class="label">Failed</span><span class="value failed">{failed}</span></div>
  <div class="item"><span class="label">Incomplete</span><span class="value incomplete">{incomplete}</span></div>
</div>

<table>
  <thead>
    <tr>
      <th>Task</th>
      <th>Status</th>
      <th>Time</th>
      <th>Duration</th>
      <th>Branch</th>
      <th>PR</th>
      <th>Summary</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
</body>
</html>"""

    # Write artifact
    artifacts_dir = Path.home() / ".agentwire" / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    filename = f"morning-report-{report_date}.html"
    report_path = artifacts_dir / filename
    report_path.write_text(html)

    if json_mode:
        _output_json({
            "success": True,
            "path": str(report_path),
            "filename": filename,
            "total": total,
            "complete": complete,
            "failed": failed,
            "incomplete": incomplete,
        })
    else:
        print(f"Report: {report_path}")
        print(f"Tasks: {total} total — {complete} complete, {failed} failed, {incomplete} incomplete")

    if open_artifact:
        subprocess.run(
            ["agentwire", "open", filename, "--title", f"Morning Report {report_date}"],
            capture_output=True,
        )

    return 0


def cmd_scheduler_events(args) -> int:
    """Show recent scheduler events from the JSONL log."""
    from .scheduler import read_events

    json_mode = getattr(args, 'json', False)
    tail = getattr(args, 'tail', 20)
    task_filter = getattr(args, 'task', None)

    events = read_events(tail=tail, task_filter=task_filter)

    if json_mode:
        _output_json({"success": True, "events": events})
        return 0

    if not events:
        print("No scheduler events.")
        return 0

    for evt in events:
        ts = evt.get("ts", "")
        # Format timestamp for display
        try:
            dt = datetime.datetime.fromisoformat(ts)
            ts_str = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            ts_str = ts[:16] if ts else "?"

        event_type = evt.get("event", "?")
        task_name = evt.get("task", "")
        session = evt.get("session", "")

        if event_type == "task_completed":
            status = evt.get("status", "?")
            duration = evt.get("duration", 0)
            summary = evt.get("summary", "")
            summary_str = f'  "{summary}"' if summary else ""
            print(f"{ts_str}  {event_type:<22} {task_name:<24} {status:<12} {duration}s{summary_str}")
        elif event_type == "task_started":
            print(f"{ts_str}  {event_type:<22} {task_name:<24} {session}")
        elif event_type == "task_skipped":
            reason = evt.get("reason", "?")
            print(f"{ts_str}  {event_type:<22} {task_name:<24} reason: {reason}")
        elif event_type == "gate_error":
            gate_type = evt.get("gate_type", "?")
            reason = evt.get("reason", "?")
            print(f"{ts_str}  {event_type:<22} {task_name:<24} {gate_type}: {reason} (failed open)")
        elif event_type == "scheduler_sleeping":
            next_task = evt.get("next_task", "?")
            sleep_s = evt.get("sleep_seconds", 0)
            print(f"{ts_str}  {event_type:<22} next: {next_task} in {int(sleep_s)}s")
        elif event_type == "scheduler_started":
            count = evt.get("task_count", 0)
            enabled = evt.get("enabled_count", 0)
            print(f"{ts_str}  {event_type:<22} {enabled}/{count} tasks enabled")
        else:
            print(f"{ts_str}  {event_type:<22} {task_name}")

    return 0


def cmd_scheduler_live(args) -> int:
    """Show live scheduler state."""
    from .scheduler import format_interval, read_live_state

    json_mode = getattr(args, 'json', False)
    watch_mode = getattr(args, 'watch', False)

    def _display_once():
        state = read_live_state()
        if not state:
            if json_mode:
                _output_json({"success": False, "error": "No live state file. Is the scheduler running?"})
            else:
                print("No live state available. Is the scheduler running?")
            return False

        if json_mode:
            _output_json({"success": True, **state})
            return True

        status = state.get("status", "unknown")
        uptime = state.get("uptime_seconds", 0)
        current = state.get("current_task")
        current_started = state.get("current_task_started")
        completed = state.get("tasks_completed", 0)
        failed = state.get("tasks_failed", 0)
        next_task = state.get("next_task")
        next_in = state.get("next_in_seconds", 0)

        print(f"Scheduler: {status} (uptime {format_interval(int(uptime))})")

        if current:
            # Calculate running time
            running_str = ""
            if current_started:
                try:
                    started_dt = datetime.datetime.fromisoformat(current_started)
                    running = int((datetime.datetime.now(datetime.timezone.utc) - started_dt).total_seconds())
                    running_str = f" (running {format_interval(running)})"
                except (ValueError, TypeError):
                    pass
            print(f"Current:   {current}{running_str}")
        else:
            print("Current:   idle")

        print(f"Completed: {completed} tasks | Failed: {failed}")

        if next_task:
            print(f"Next:      {next_task} (in {format_interval(int(next_in))})")
        elif not current:
            print("Next:      nothing due")

        return True

    if watch_mode:
        import os
        try:
            while True:
                os.system("clear")
                _display_once()
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
    else:
        _display_once()
        return 0


def cmd_scheduler_dashboard(args) -> int:
    """Generate a live HTML dashboard and announce it as a portal artifact."""
    no_open = getattr(args, 'no_open', False)

    html = _generate_dashboard_html()

    # Write to artifacts
    artifacts_dir = Path.home() / ".agentwire" / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    dashboard_path = artifacts_dir / "scheduler-dashboard.html"
    dashboard_path.write_text(html)
    print(f"Dashboard written to {dashboard_path}")

    if not no_open:
        subprocess.run(
            ["agentwire", "open", "scheduler-dashboard.html", "--title", "Scheduler Dashboard"],
            capture_output=True,
        )

    return 0


def _generate_dashboard_html() -> str:
    """Generate a live scheduler dashboard that fetches data from REST APIs."""

    return '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Scheduler Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 20px; background: #1a1a2e; color: #e0e0e0; }
  h1 { color: #00d4ff; margin-bottom: 4px; }
  h2 { color: #8892b0; margin-top: 24px; margin-bottom: 8px; font-size: 1.1em; }
  .meta { color: #555; font-size: 0.82em; margin-bottom: 16px; }
  .status-bar { display: flex; gap: 24px; padding: 12px 16px; background: #16213e; border-radius: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .status-bar .item { display: flex; flex-direction: column; }
  .status-bar .label { color: #556; font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.5px; }
  .status-bar .value { color: #e0e0e0; font-size: 1.1em; font-weight: 600; }
  .status-bar .value.running { color: #00c853; }
  .status-bar .value.idle { color: #8892b0; }
  .status-bar .value.stopped { color: #ff5252; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #2a2a4a; font-size: 0.9em; }
  th { background: #16213e; color: #00d4ff; font-weight: 600; position: sticky; top: 0; }
  tr:hover { background: #16213e; }
  .complete { color: #00c853; }
  .failed, .timeout { color: #ff5252; }
  .never { color: #555; }
  .lock_conflict, .incomplete { color: #ffa726; }
  .disabled { opacity: 0.4; }
  .evt-task_completed { color: #00c853; }
  .evt-task_started { color: #42a5f5; }
  .evt-task_skipped { color: #ffa726; }
  .evt-scheduler_sleeping { color: #555; }
  .evt-scheduler_started { color: #00d4ff; }
  .pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #00c853; margin-right: 6px; animation: pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
</style>
</head>
<body>
<h1>Scheduler Dashboard</h1>
<p class="meta">Live &mdash; polls every 10s, instant updates via WebSocket</p>

<div class="status-bar" id="status-bar">
  <div class="item"><span class="label">Status</span><span class="value" id="sb-status">&mdash;</span></div>
  <div class="item"><span class="label">Uptime</span><span class="value" id="sb-uptime">&mdash;</span></div>
  <div class="item"><span class="label">Current</span><span class="value" id="sb-current">&mdash;</span></div>
  <div class="item"><span class="label">Completed</span><span class="value" id="sb-completed">&mdash;</span></div>
  <div class="item"><span class="label">Failed</span><span class="value" id="sb-failed">&mdash;</span></div>
  <div class="item"><span class="label">Next</span><span class="value" id="sb-next">&mdash;</span></div>
</div>

<h2>Task Board</h2>
<table>
  <thead><tr><th>Task</th><th>Schedule</th><th>Last Run</th><th>Status</th><th>Duration</th><th>Overdue</th><th>Runs</th></tr></thead>
  <tbody id="board-body"></tbody>
</table>

<h2>Recent Events</h2>
<table>
  <thead><tr><th style="width:70px">Time</th><th style="width:160px">Event</th><th style="width:180px">Task</th><th>Details</th></tr></thead>
  <tbody id="events-body"></tbody>
</table>

<script>
const BASE = location.origin;
const WS_URL = BASE.replace(/^http/, "ws") + "/ws";

function fmtInterval(s) {
  s = Math.round(s);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return m ? h + "h" + m + "m" : h + "h";
}

function fmtTime(iso) {
  try { return new Date(iso).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}); }
  catch(e) { return iso ? iso.slice(11, 19) : "?"; }
}

function esc(s) {
  var d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

async function refreshLive() {
  try {
    var r = await fetch(BASE + "/api/scheduler/live");
    if (!r.ok) throw new Error(r.status);
    var d = await r.json();
    var el = document.getElementById("sb-status");
    if (d.current_task) { el.textContent = "running"; el.className = "value running"; }
    else if (d.status === "running") { el.innerHTML = "<span class=\"pulse\"></span>idle"; el.className = "value idle"; }
    else { el.textContent = d.status || "stopped"; el.className = "value stopped"; }
    document.getElementById("sb-uptime").textContent = fmtInterval(d.uptime_seconds || 0);
    if (d.current_task) {
      var run = "";
      if (d.current_task_started) {
        var elapsed = (Date.now() - new Date(d.current_task_started).getTime()) / 1000;
        run = " (" + fmtInterval(elapsed) + ")";
      }
      document.getElementById("sb-current").textContent = d.current_task + run;
    } else { document.getElementById("sb-current").textContent = "\u2014"; }
    document.getElementById("sb-completed").textContent = d.tasks_completed != null ? d.tasks_completed : "\u2014";
    document.getElementById("sb-failed").textContent = d.tasks_failed != null ? d.tasks_failed : "\u2014";
    if (d.next_task) {
      document.getElementById("sb-next").textContent = d.next_task + " (in " + fmtInterval(d.next_in_seconds || 0) + ")";
    } else { document.getElementById("sb-next").textContent = "\u2014"; }
  } catch(e) {
    document.getElementById("sb-status").textContent = "offline";
    document.getElementById("sb-status").className = "value stopped";
  }
}

async function refreshBoard() {
  try {
    var r = await fetch(BASE + "/api/scheduler/board");
    if (!r.ok) return;
    var d = await r.json();
    var tbody = document.getElementById("board-body");
    tbody.innerHTML = (d.tasks || []).map(function(t) {
      var cls = t.enabled ? t.last_status : "disabled";
      var label = esc(t.label) + (t.enabled ? "" : " <span style=\"color:#555\">[off]</span>");
      return "<tr class=\"" + cls + "\">" +
        "<td>" + label + "</td>" +
        "<td>" + esc(t.schedule_str || "?") + "</td>" +
        "<td>" + esc(t.last_run) + "</td>" +
        "<td class=\"" + esc(t.last_status) + "\">" + esc(t.last_status) + "</td>" +
        "<td>" + t.last_duration + "s</td>" +
        "<td>" + esc(t.overdue_str) + "</td>" +
        "<td>" + t.run_count + "</td></tr>";
    }).join("");
  } catch(e) {}
}

async function refreshEvents() {
  try {
    var r = await fetch(BASE + "/api/scheduler/events?tail=30");
    if (!r.ok) return;
    var d = await r.json();
    var evts = (d.events || []).slice().reverse();
    var tbody = document.getElementById("events-body");
    tbody.innerHTML = evts.map(function(evt) {
      var ts = fmtTime(evt.ts);
      var etype = evt.event || "?";
      var task = evt.task || "";
      var detail = "";
      if (etype === "task_completed") detail = esc(evt.status) + " \u2014 " + esc(evt.summary);
      else if (etype === "task_skipped") detail = esc(evt.reason);
      else if (etype === "scheduler_sleeping") detail = "next: " + esc(evt.next_task) + " in " + Math.round(evt.sleep_seconds || 0) + "s";
      else if (etype === "task_started") detail = esc(evt.session);
      else if (etype === "scheduler_started") detail = (evt.enabled_count || 0) + "/" + (evt.task_count || 0) + " tasks enabled";
      return "<tr><td>" + ts + "</td><td class=\"evt-" + esc(etype) + "\">" + esc(etype) + "</td><td>" + esc(task) + "</td><td>" + detail + "</td></tr>";
    }).join("");
  } catch(e) {}
}

function connectWS() {
  try {
    var ws = new WebSocket(WS_URL);
    ws.onmessage = function(e) {
      try {
        var msg = JSON.parse(e.data);
        if (msg.type === "scheduler_update") { refreshLive(); refreshBoard(); refreshEvents(); }
      } catch(ex) {}
    };
    ws.onclose = function() { setTimeout(connectWS, 5000); };
    ws.onerror = function() { ws.close(); };
  } catch(e) {}
}

refreshLive(); refreshBoard(); refreshEvents();
setInterval(refreshLive, 10000);
setInterval(refreshBoard, 10000);
setInterval(refreshEvents, 10000);
connectWS();
</script>
</body>
</html>'''


def register_scheduler_parser(subparsers) -> None:
    """Register the scheduler command group."""
    scheduler_parser = subparsers.add_parser(
        "scheduler",
        help="Manage the task scheduler",
        description=(
            "Centralized daemon that dispatches tasks across projects on a shared cadence. "
            "Tasks in ~/.agentwire/scheduler.yaml are either ensure tasks (task: + session:) "
            "or workflow tasks (workflow: + inputs:) — the scheduler routes each automatically. "
            "See docs/wiki/scheduling/scheduled-workloads.md."
        ),
    )
    scheduler_subparsers = scheduler_parser.add_subparsers(dest="scheduler_command")

    # scheduler start
    sched_start = scheduler_subparsers.add_parser("start", help="Start scheduler daemon")
    sched_start.set_defaults(func=cmd_scheduler_start)

    # scheduler serve (foreground, for tmux)
    sched_serve = scheduler_subparsers.add_parser("serve", help="Run scheduler in foreground")
    sched_serve.add_argument(
        "--force", action="store_true",
        help="Start even when another daemon is recorded as live (#873)",
    )
    sched_serve.set_defaults(func=cmd_scheduler_serve)

    # scheduler stop
    sched_stop = scheduler_subparsers.add_parser("stop", help="Stop scheduler")
    sched_stop.set_defaults(func=cmd_scheduler_stop)

    # scheduler status
    sched_status = scheduler_subparsers.add_parser("status", help="Check scheduler status")
    sched_status.add_argument("--json", action="store_true", help="Output JSON")
    sched_status.set_defaults(func=cmd_scheduler_status)

    # scheduler board
    sched_board = scheduler_subparsers.add_parser("board", help="Show task board with overdue scores")
    sched_board.add_argument("--json", action="store_true", help="Output JSON")
    sched_board.set_defaults(func=cmd_scheduler_board)

    # scheduler run <name>
    sched_run = scheduler_subparsers.add_parser("run", help="Force-run a task now")
    sched_run.add_argument("name", help="Task name from board")
    sched_run.add_argument("--json", action="store_true", help="Output JSON")
    sched_run.set_defaults(func=cmd_scheduler_run)

    # scheduler enable <name>
    sched_enable = scheduler_subparsers.add_parser("enable", help="Enable a task")
    sched_enable.add_argument("name", help="Task name")
    sched_enable.set_defaults(func=cmd_scheduler_enable)

    # scheduler disable <name>
    sched_disable = scheduler_subparsers.add_parser("disable", help="Disable a task")
    sched_disable.add_argument("name", help="Task name")
    sched_disable.set_defaults(func=cmd_scheduler_disable)

    # scheduler history
    sched_history = scheduler_subparsers.add_parser("history", help="Show recent run history")
    sched_history.add_argument("--json", action="store_true", help="Output JSON")
    sched_history.set_defaults(func=cmd_scheduler_history)

    # scheduler events
    sched_events = scheduler_subparsers.add_parser("events", help="Show recent scheduler events")
    sched_events.add_argument("--json", action="store_true", help="Output JSON")
    sched_events.add_argument("--tail", type=int, default=20, help="Number of events (default: 20)")
    sched_events.add_argument("--task", help="Filter by task name")
    sched_events.set_defaults(func=cmd_scheduler_events)

    # scheduler live
    sched_live = scheduler_subparsers.add_parser("live", help="Show live scheduler state")
    sched_live.add_argument("--json", action="store_true", help="Output JSON")
    sched_live.add_argument("--watch", action="store_true", help="Re-read every 2s")
    sched_live.set_defaults(func=cmd_scheduler_live)

    # scheduler dashboard
    sched_dashboard = scheduler_subparsers.add_parser("dashboard", help="Open scheduler dashboard")
    sched_dashboard.add_argument("--no-open", action="store_true", help="Generate HTML without announcing it in the portal")
    sched_dashboard.set_defaults(func=cmd_scheduler_dashboard)

    # scheduler report
    sched_report = scheduler_subparsers.add_parser("report", help="Generate morning report of recent task runs")
    sched_report.add_argument("--since", default="8h", metavar="DURATION", help="Time window (e.g. 8h, 12h, 1d) default: 8h")
    sched_report.add_argument("--artifact", action="store_true", help="Announce report as a click-to-open portal notification")
    sched_report.add_argument("--json", action="store_true", help="Output JSON")
    sched_report.set_defaults(func=cmd_scheduler_report)
