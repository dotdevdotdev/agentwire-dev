"""CLI for Claude Code session history — ``agentwire history ...``.

Lists/shows past conversations (local or remote) and resumes one. A resume
always forks (``claude --resume <id> --fork-session``) and routes through
``resolve_roles`` with the derived kind so a zero-config resume gets the same
orchestrator etiquette a fresh ``agentwire new`` would (#316).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .core import (
    _get_machine_config,
    _notify_portal_sessions_changed,
    _output_json,
    _output_result,
    _run_remote,
    build_agent_command,
    format_relative_time,
    load_config,
)
from .project_config import ProjectConfig, load_project_config
from .roles import (
    derive_session_kind,
    inject_soul,
    load_roles,
    resolve_roles,
)
from .worktree import tmux_safe_name


def cmd_history_list(args) -> int:
    """List conversation history for a project."""
    from .history import get_history

    # Determine project path
    if args.project:
        project_path = Path(args.project).resolve()
        if not project_path.exists():
            print(f"Project path not found: {project_path}", file=sys.stderr)
            return 1
    else:
        # Check if cwd is a tracked project
        config = load_project_config()
        if config is None:
            print("Not in a tracked project directory.", file=sys.stderr)
            print("Use --project <path> or run from a directory with .agentwire.yml", file=sys.stderr)
            return 1
        project_path = Path.cwd().resolve()

    # Get history
    sessions = get_history(
        project_path=str(project_path),
        machine=args.machine,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0

    if not sessions:
        print(f"No history found for {project_path}")
        return 0

    print(f"Session history for {project_path.name} ({len(sessions)} sessions):")
    print()

    for session in sessions:
        session_id = session.get("sessionId", "")
        short_id = session_id[:8] if session_id else "?"
        timestamp = session.get("timestamp", 0)
        relative_time = format_relative_time(timestamp) if timestamp else "unknown"
        message_count = session.get("messageCount", 0)
        last_summary = session.get("lastSummary") or session.get("firstMessage", "")

        # Truncate summary for display
        if last_summary and len(last_summary) > 60:
            last_summary = last_summary[:57] + "..."

        print(f"  {short_id}  {relative_time:>15}  ({message_count} msgs)")
        if last_summary:
            print(f"           {last_summary}")
        print()

    return 0


def cmd_history_show(args) -> int:
    """Show details for a specific session."""
    from .history import get_session_detail

    session_id = args.session_id

    # Get session details
    detail = get_session_detail(
        session_id=session_id,
        machine=args.machine,
    )

    if detail is None:
        print(f"Session not found: {session_id}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(detail, indent=2))
        return 0

    # Display formatted output
    full_id = detail.get("sessionId", "?")
    message_count = detail.get("messageCount", 0)
    git_branch = detail.get("gitBranch")
    first_message = detail.get("firstMessage", "")
    summaries = detail.get("summaries", [])
    timestamps = detail.get("timestamps", {})

    start_ts = timestamps.get("start")
    end_ts = timestamps.get("end")

    print(f"Session: {full_id}")
    print()

    if start_ts:
        from datetime import datetime
        start_dt = datetime.fromtimestamp(start_ts / 1000)
        print(f"  Started:  {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    if end_ts:
        from datetime import datetime
        end_dt = datetime.fromtimestamp(end_ts / 1000)
        print(f"  Last msg: {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    print(f"  Messages: {message_count}")

    if git_branch:
        print(f"  Branch:   {git_branch}")

    print()

    if first_message:
        # Truncate for display
        preview = first_message[:200] + "..." if len(first_message) > 200 else first_message
        print("First message:")
        print(f"  {preview}")
        print()

    if summaries:
        print(f"Summaries ({len(summaries)}):")
        for i, summary in enumerate(summaries, 1):
            # Truncate each summary
            if len(summary) > 100:
                summary = summary[:97] + "..."
            print(f"  {i}. {summary}")
        print()

    return 0


def cmd_history_resume(args) -> int:
    """Resume a Claude Code session.

    Creates a new tmux session and runs: `claude --resume <session-id> --fork-session`

    Flags are applied based on the project's .agentwire.yml config.
    """
    session_id = args.session_id
    name = getattr(args, 'name', None)
    machine_id = getattr(args, 'machine', 'local')
    project_path_str = args.project
    json_mode = getattr(args, 'json', False)

    # Resolve prefix to full UUID for Claude Code sessions
    from .history import resolve_session_id
    resolved = resolve_session_id(session_id, machine_id)
    if resolved:
        session_id = resolved

    # Resolve project path
    project_path = Path(project_path_str).expanduser().resolve()

    # Load project config for type and roles
    project_config = load_project_config(project_path)
    if project_config is None:
        project_config = ProjectConfig(posture="bypass", roles=[])

    # Generate session name if not provided
    if not name:
        # tmux reads `.` and `:` as its address separators (`session.window`,
        # `session:window`) and rewrites BOTH to `_`, so a project dir carrying
        # one (`~/.claude`) yields the session `_claude-fork-1`, never
        # `.claude-fork-1`. One implementation of that mapping, shared with
        # every creation path (#868/#870/#878) — the uniqueness probe below must
        # run on the sanitized name or it checks for a session tmux could not
        # have made.
        base_name = tmux_safe_name(project_path.name)
        # Find unique name with -fork-N suffix
        name = f"{base_name}-fork-1"
        counter = 1
        while True:
            # Check if session exists locally
            check_result = subprocess.run(
                ["tmux", "has-session", "-t", f"={name}"],
                capture_output=True
            )
            if check_result.returncode != 0:
                break  # Session doesn't exist, use this name
            counter += 1
            name = f"{base_name}-fork-{counter}"
    else:
        # Same mapping on an operator-supplied --name: left raw, the
        # has-session probe and every later `-t <name>` address a *window* that
        # doesn't exist, while we report a session name tmux never created.
        name = tmux_safe_name(name)

    # Route through resolve_roles with the derived kind so a resumed session
    # carries its kind's intrinsic etiquette. A history-resume has no branch, so
    # it's always an orchestrator — a zero-config resume now gets the same
    # orchestrator etiquette a fresh `agentwire new` would, instead of an empty
    # role list. Same contract as cmd_session_recreate/fork (#311/#315).
    kind = derive_session_kind(False)
    project_roles = list(project_config.roles) if project_config.roles else None
    role_names = resolve_roles(kind, project_roles=project_roles)
    role_names = inject_soul(role_names, load_config())
    roles = None
    if role_names:
        loaded, missing = load_roles(role_names, project_path)
        if not missing and loaded:
            roles = loaded

    # Build the resume command through the ONE flag-builder (#729) so a resumed
    # session gets EXACTLY the posture flags a fresh one would — including auto's
    # tool-allows injection, which the old to_cli_flags() path silently dropped.
    # resume_session_id inserts --resume/--fork-session right after `claude`.
    # The role temp-file path (if any) is already inlined in agent.command via
    # --append-system-prompt "$(<...)"; it persists (delete=False) for claude to
    # read at launch, so there's nothing to hold onto here.
    agent = build_agent_command(project_config.posture, roles, resume_session_id=session_id)
    agent_cmd = agent.command

    if machine_id and machine_id != "local":
        # Remote machine
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        remote_path = str(project_path)

        # Check if session already exists on remote
        check_cmd = f"tmux has-session -t ={shlex.quote(name)} 2>/dev/null"
        result = _run_remote(machine_id, check_cmd)
        if result.returncode == 0:
            return _output_result(False, json_mode, f"Session '{name}' already exists on {machine_id}")

        # Create remote tmux session and send claude command
        create_cmd = (
            f"tmux new-session -d -s {shlex.quote(name)} -c {shlex.quote(remote_path)} && "
            f"tmux send-keys -t {shlex.quote(name)} 'cd {shlex.quote(remote_path)}' Enter && "
            f"sleep 0.1 && "
            f"tmux send-keys -t {shlex.quote(name)} {shlex.quote(agent_cmd)} Enter"
        )

        result = _run_remote(machine_id, create_cmd)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create remote session: {result.stderr}")

        if json_mode:
            _output_json({
                "success": True,
                "session": f"{name}@{machine_id}",
                "resumed_from": session_id,
                "path": remote_path,
                "machine": machine_id,
                "posture": project_config.posture,
            })
        else:
            host = machine.get('host', machine_id)
            print(f"Resumed session '{name}' on {machine_id} (forked from {session_id})")
            print(f"Attach via portal or: ssh {host} -t tmux attach -t {name}")

        _notify_portal_sessions_changed()
        return 0

    # Local session
    if not project_path.exists():
        return _output_result(False, json_mode, f"Project path does not exist: {project_path}")

    # Check if session already exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"],
        capture_output=True
    )
    if result.returncode == 0:
        return _output_result(False, json_mode, f"Session '{name}' already exists. Choose a different name with --name.")

    # Create new tmux session
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(project_path)],
        check=True
    )

    # Ensure Claude starts in correct directory
    subprocess.run(
        ["tmux", "send-keys", "-t", name, f"cd {shlex.quote(str(project_path))}", "Enter"],
        check=True
    )
    time.sleep(0.1)

    # Send the claude resume command
    subprocess.run(
        ["tmux", "send-keys", "-t", name, agent_cmd, "Enter"],
        check=True
    )

    if json_mode:
        _output_json({
            "success": True,
            "session": name,
            "resumed_from": session_id,
            "path": str(project_path),
            "machine": None,
            "posture": project_config.posture,
        })
    else:
        print(f"Resumed session '{name}' (forked from {session_id})")
        print(f"Project: {project_path}")
        print(f"Attach with: tmux attach -t {name}")

    _notify_portal_sessions_changed()
    return 0


def register_history_parser(subparsers) -> None:
    history_parser = subparsers.add_parser("history", help="Claude Code session history")
    history_subparsers = history_parser.add_subparsers(dest="history_command")

    # history list
    history_list = history_subparsers.add_parser("list", help="List conversation history")
    history_list.add_argument("--project", "-p", help="Project path (defaults to cwd)")
    history_list.add_argument("--machine", "-m", default="local", help="Machine ID")
    history_list.add_argument("--limit", "-n", type=int, default=20, help="Max results")
    history_list.add_argument("--json", action="store_true", help="JSON output")
    history_list.set_defaults(func=cmd_history_list)

    # history show <session_id>
    history_show = history_subparsers.add_parser("show", help="Show session details")
    history_show.add_argument("session_id", help="Session ID to show")
    history_show.add_argument("--machine", "-m", default="local", help="Machine ID")
    history_show.add_argument("--json", action="store_true", help="JSON output")
    history_show.set_defaults(func=cmd_history_show)

    # history resume <session_id>
    history_resume = history_subparsers.add_parser("resume", help="Resume a session (always forks)")
    history_resume.add_argument("session_id", help="Session ID to resume")
    history_resume.add_argument("--name", "-n", help="New tmux session name")
    history_resume.add_argument("--machine", "-m", default="local", help="Machine ID")
    history_resume.add_argument("--project", "-p", required=True, help="Project path")
    history_resume.add_argument("--json", action="store_true", help="JSON output")
    history_resume.set_defaults(func=cmd_history_resume)
