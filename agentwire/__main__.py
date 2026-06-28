"""CLI entry point for AgentWire."""

import argparse
import datetime
import importlib.resources
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# Load .env files (project first, then global config)
load_dotenv()  # .env in current directory
load_dotenv(Path.home() / ".agentwire" / ".env")  # Global config

from . import (  # noqa: E402  # must follow load_dotenv() above
    __version__,
    cli_safety,
    pane_manager,
)
from .core import (  # noqa: E402,F401  # E402: must follow load_dotenv(); F401: re-exported moved helpers
    _UNATTENDED_ENV_KEYS,
    CONFIG_DIR,
    KIND_DEFAULT_POSTURE,
    AgentCommand,
    _add_posture_harness_flags,
    _build_tmux_env_flags,
    _build_tmux_env_flags_shell,
    _check_portal_health,
    _check_tmux_installed,
    _default_portal_url,
    _display_parent,
    _get_agentwire_path,
    _get_all_machines,
    _get_machine_config,
    _get_portal_url,
    _get_session_project_path,
    _git_behind_origin,
    _install_global_tmux_hooks,
    _notify_portal_sessions_changed,
    _output_json,
    _output_result,
    _parse_session_target,
    _portal_auth_headers,
    _post_desktop_notification,
    _record_session_creator,
    _resolve_session_type_from_args,
    _run_remote,
    _set_session_name_env,
    _start_portal_local,
    _tmux_global_option,
    _with_unattended_env,
    build_agent_command,
    check_pip_environment,
    check_python_version,
    format_relative_time,
    generate_certs,
    get_kokoro_session_name,
    get_portal_session_name,
    get_source_dir,
    get_stt_session_name,
    get_tts_session_name,
    inject_session_env,
    load_config,
    load_session_metadata,
    parse_env_args,
    store_session_metadata,
    tmux_session_exists,
    tmux_session_has_agent,
    wait_for_shell_prompt,
)
from .project_config import (  # noqa: E402  # must follow load_dotenv() above
    ProjectConfig,
    SessionType,
    detect_default_agent_type,
    get_parent_from_config,
    load_project_config,
)
from .roles import (  # noqa: E402  # must follow load_dotenv() above
    derive_session_kind,
    inject_soul,
    load_roles,
    merge_roles,
    resolve_roles,
)


def cmd_notify_parent(args) -> int:
    """Notify parent session (worker→orchestrator communication).

    Sends a prefixed text message to the parent session via tmux.
    The parent is determined from .agentwire.yml or --to flag.

    This is for session hierarchy communication. For outbound notifications
    to the user across devices, use `agentwire email` or `agentwire quo`.

    Notification targets (in priority order):
    1. --to SESSION if specified
    2. parent from .agentwire.yml if exists
    3. pane 0 of current session (if in worker pane)

    Examples:
        agentwire notify "Worker 1 completed task"
        agentwire notify --to agentwire "Build finished"
    """
    text = " ".join(args.text) if args.text else ""
    json_mode = getattr(args, 'json', False)

    if not text:
        return _output_result(False, json_mode, "Usage: agentwire notify-parent <message>")

    target_session = getattr(args, 'to', None)
    current_session = pane_manager.get_current_session()
    current_pane = pane_manager.get_current_pane_index()

    # If no explicit target, try parent from config
    if not target_session:
        parent = get_parent_from_config()
        if parent:
            target_session = parent

    # Build notification message (--raw sends verbatim — queued messages
    # already carry their own [WORKER SUMMARY ...] / [PROMPT ...] headers)
    if getattr(args, 'raw', False):
        notification = text
    else:
        source = current_session or "unknown"
        if current_pane is not None and current_pane > 0:
            notification = f"[NOTIFY from {source} pane {current_pane}] {text}"
        else:
            notification = f"[NOTIFY from {source}] {text}"

    if target_session:
        if target_session == current_session and current_pane == 0:
            return _output_result(False, json_mode, "Cannot notify own pane")
    elif current_pane is not None and current_pane > 0 and current_session:
        target_session = current_session
    else:
        return _output_result(
            False, json_mode,
            "No target session (set 'parent' in .agentwire.yml or use --to)")

    # safe_deliver refuses targets where a paste could do damage (live
    # dialog on screen, bare shell, parked session) and verifies the paste
    # actually landed. Callers (queue processor) retry on failure.
    from agentwire import prompt_router

    delivered, reason = prompt_router.safe_deliver(target_session, 0, notification)
    if json_mode:
        _output_json({
            "success": delivered,
            "target": target_session,
            "delivered": delivered,
            "reason": reason if not delivered else None,
        })
        return 0 if delivered else 1
    if not delivered:
        print(f"Notification not delivered to {target_session}: {reason}", file=sys.stderr)
        return 1
    if not getattr(args, 'quiet', False):
        print(f"Notified {target_session}")
    return 0


def cmd_open(args) -> int:
    """Open a URL or local file as an artifact window in the portal.

    Examples:
        agentwire open dashboard.html --title "Dashboard"
        agentwire open https://example.com --title "External"
        agentwire open test.html --artifact-id my-test --json
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    url = args.url
    title = args.title
    artifact_id = getattr(args, 'artifact_id', None)
    json_output = getattr(args, 'json', False)

    portal_url = _get_portal_url()

    body = {
        "type": "artifact",
        "url": url,
        "title": title,
    }
    if artifact_id:
        body["artifact_id"] = artifact_id

    try:
        resp = requests.post(
            f"{portal_url}/api/desktop/window/open",
            json=body,
            headers=_portal_auth_headers(),
            verify=False,
            timeout=10,
        )
        data = resp.json()

        if json_output:
            print(json.dumps(data))
        elif data.get("success"):
            print(f"Opened artifact window: {title} (id: {data.get('window_id', 'unknown')})")
        else:
            print(f"Failed: {data.get('error', 'Unknown error')}", file=sys.stderr)
            return 1

    except requests.exceptions.ConnectionError:
        msg = "Portal not reachable. Is it running? (agentwire portal status)"
        if json_output:
            print(json.dumps({"success": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1
    except Exception as e:
        if json_output:
            print(json.dumps({"success": False, "error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def cmd_notify_user(args) -> int:
    """Show the human a desktop toast on the portal (notify-user)."""
    text = " ".join(args.text) if args.text else ""
    json_mode = getattr(args, "json", False)
    if not text.strip():
        return _output_result(False, json_mode, "Usage: agentwire notify-user <text>")
    ok = _post_desktop_notification(
        text, session=getattr(args, "session", None),
        priority=getattr(args, "priority", "normal"),
    )
    return _output_result(ok, json_mode,
                          "Toast posted." if ok else "Failed to post toast (portal not reachable?)")


def cmd_research(args) -> int:
    """Resolve (or ensure) the Briefing Mode research dropbox for a session."""
    from .research import ensure_research_dir, research_dir

    json_mode = getattr(args, "json", False)
    session = getattr(args, "session", None) or pane_manager.get_current_session()
    if not session:
        return _output_result(False, json_mode, "No session (use -s or run inside a session)")
    sub = getattr(args, "research_command", None)
    path = ensure_research_dir(session) if sub == "ensure" else research_dir(session)
    if json_mode:
        _output_json({"success": True, "session": session, "path": str(path), "exists": path.exists()})
        return 0
    print(str(path))
    return 0


def cmd_wiki(args) -> int:
    """Deterministic mechanical ops for the LLM wiki (status/query/lint/new/done)."""
    from . import wiki

    json_mode = getattr(args, "json", False)
    root = getattr(args, "root", None)
    sub = getattr(args, "wiki_command", None)

    if sub is None:
        return _output_result(False, json_mode, "Specify a subcommand: status, query, lint, new, done")

    if sub == "status":
        data = wiki.status(root)
        if json_mode:
            _output_json({"success": True, **data})
            return 0
        print(wiki._status_text(data))
        return 0

    if sub == "query":
        results = wiki.query(args.query, root, limit=args.limit)
        if json_mode:
            _output_json({"success": True, "results": results})
            return 0
        if not results:
            print("No matching pages.")
            return 0
        for r in results:
            print(f"  [{r['score']:>3}] {r['rel']}\n        {r['snippet']}")
        return 0

    if sub == "lint":
        findings = wiki.lint(root)
        if json_mode:
            _output_json({"success": True, "findings": findings, "count": len(findings)})
        else:
            print(wiki._format_lint_text(findings))
        return 1 if (findings and getattr(args, "strict", False)) else 0

    if sub == "new":
        try:
            dest = wiki.new_page(args.category, args.name, root, title=getattr(args, "title", None))
        except (ValueError, FileExistsError) as e:
            return _output_result(False, json_mode, str(e))
        return _output_result(True, json_mode, f"Created {dest}", path=str(dest))

    if sub == "done":
        try:
            dest = wiki.done(args.rawfile, root)
        except (ValueError, FileNotFoundError) as e:
            return _output_result(False, json_mode, str(e))
        return _output_result(True, json_mode, f"Archived → {dest}", path=str(dest))

    return _output_result(False, json_mode, f"Unknown wiki subcommand: {sub}")


def cmd_notify(args) -> int:
    """Send a notification to the portal about session/pane state changes.

    Called by tmux hooks to notify the portal when sessions are created/closed,
    panes are created/killed, clients attach/detach, sessions are renamed, etc.
    The portal broadcasts these events to connected dashboard clients for real-time
    UI updates.
    """
    event = args.event
    session = getattr(args, 'session', None)
    pane = getattr(args, 'pane', None)
    pane_id = getattr(args, 'pane_id', None)
    old_name = getattr(args, 'old_name', None)
    new_name = getattr(args, 'new_name', None)
    json_mode = getattr(args, 'json', False)

    if not event:
        return _output_result(False, json_mode, "Event is required")

    portal_url = _get_portal_url()
    if not portal_url:
        return _output_result(False, json_mode, "Portal URL not configured")

    # Build payload
    payload = {"event": event}
    if session:
        payload["session"] = session
    if pane is not None:
        payload["pane"] = pane
    if pane_id is not None:
        payload["pane_id"] = pane_id
    if old_name is not None:
        payload["old_name"] = old_name
    if new_name is not None:
        payload["new_name"] = new_name

    try:
        # Use urllib to avoid requests dependency in core CLI

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{portal_url}/api/notify",
            data=data,
            headers={"Content-Type": "application/json", **_portal_auth_headers()},
            method="POST"
        )

        # Disable SSL verification for self-signed certs
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=5, context=ctx) as response:
            result = json.loads(response.read().decode())

        if result.get("success"):
            if json_mode:
                _output_json({"success": True, "event": event, "session": session,
                              "clients": result.get("clients", 0)})
            return 0
        else:
            return _output_result(False, json_mode, result.get("error", "Unknown error"))

    except Exception as e:
        # Don't fail loudly - hooks run in background and shouldn't block tmux
        if json_mode:
            _output_json({"success": False, "error": str(e)})
        return 1


# === History Commands ===

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
        project_config = ProjectConfig(type=SessionType.CLAUDE_BYPASS, roles=[])

    # Generate session name if not provided
    if not name:
        base_name = project_path.name.replace(".", "_")
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

    # Build resume command
    temp_file = None
    cmd_parts = ["claude", "--resume", session_id, "--fork-session"]
    cmd_parts.extend(project_config.type.to_cli_flags())

    # Route through resolve_roles with the derived kind so a resumed session
    # carries its kind's intrinsic etiquette. A history-resume has no branch, so
    # it's always an orchestrator — a zero-config resume now gets the same
    # orchestrator etiquette a fresh `agentwire new` would, instead of an empty
    # role list. Same contract as cmd_session_recreate/fork (#311/#315).
    kind = derive_session_kind(False)
    project_roles = list(project_config.roles) if project_config.roles else None
    role_names = resolve_roles(kind, project_roles=project_roles)
    role_names = inject_soul(role_names, load_config())
    if role_names:
        roles, missing = load_roles(role_names, project_path)
        if not missing and roles:
            merged = merge_roles(roles)
            if merged.tools:
                cmd_parts.append("--tools")
                cmd_parts.extend(sorted(merged.tools))
            if merged.disallowed_tools:
                cmd_parts.append("--disallowedTools")
                cmd_parts.extend(sorted(merged.disallowed_tools))
            if merged.instructions:
                # Write to temp file to avoid shell escaping issues
                f = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
                f.write(merged.instructions)
                f.close()
                temp_file = f.name
                cmd_parts.append(f'--append-system-prompt "$(<{temp_file})"')

    agent_cmd = " ".join(cmd_parts)

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
                "type": project_config.type.value,
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
            "type": project_config.type.value,
        })
    else:
        print(f"Resumed session '{name}' (forked from {session_id})")
        print(f"Project: {project_path}")
        print(f"Attach with: tmux attach -t {name}")

    _notify_portal_sessions_changed()
    return 0


# === Machine Commands ===

def cmd_machine_add(args) -> int:
    """Add a machine to the AgentWire network."""
    machine_id = args.machine_id
    host = args.host or machine_id  # Default host to id if not specified
    user = args.user
    projects_dir = args.projects_dir

    machines_file = CONFIG_DIR / "machines.json"
    machines_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing machines
    machines = []
    if machines_file.exists():
        try:
            with open(machines_file) as f:
                machines = json.load(f).get("machines", [])
        except (json.JSONDecodeError, IOError):
            pass

    # Check for duplicate ID
    if any(m.get("id") == machine_id for m in machines):
        print(f"Machine '{machine_id}' already exists", file=sys.stderr)
        return 1

    # Build machine entry
    new_machine = {"id": machine_id, "host": host}
    if user:
        new_machine["user"] = user
    if projects_dir:
        new_machine["projects_dir"] = projects_dir

    machines.append(new_machine)

    # Save
    with open(machines_file, "w") as f:
        json.dump({"machines": machines}, f, indent=2)
        f.write("\n")

    print(f"Added machine '{machine_id}'")
    print(f"  Host: {host}")
    if user:
        print(f"  User: {user}")
    if projects_dir:
        print(f"  Projects: {projects_dir}")
    print()
    print("Next steps:")
    print("  1. Ensure SSH access: ssh", f"{user}@{host}" if user else host)
    print("  2. Restart portal: agentwire portal stop && agentwire portal start")
    print()
    print("Remote session management uses plain SSH — no tunnel needed. To reach")
    print("the portal from another network, bring your own tunnel (cloudflared/")
    print("tailscale); see docs/wiki/deployment/remote-access.md.")
    print()
    print("For full setup guide, run: /machine-setup in a Claude session")

    return 0


def cmd_machine_remove(args) -> int:
    """Remove a machine from the AgentWire network."""
    machine_id = args.machine_id

    machines_file = CONFIG_DIR / "machines.json"

    # Step 1: Load and check machines.json
    if not machines_file.exists():
        print(f"No machines.json found at {machines_file}", file=sys.stderr)
        return 1

    try:
        with open(machines_file) as f:
            machines_data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Invalid machines.json: {e}", file=sys.stderr)
        return 1

    machines = machines_data.get("machines", [])
    machine = next((m for m in machines if m.get("id") == machine_id), None)

    if not machine:
        print(f"Machine '{machine_id}' not found in machines.json", file=sys.stderr)
        print(f"Available machines: {', '.join(m.get('id', '?') for m in machines)}")
        return 1

    host = machine.get("host", machine_id)

    print(f"Removing machine '{machine_id}' (host: {host})...")
    print()

    # Step 3: Remove from machines.json
    print("Updating machines.json...")
    machines_data["machines"] = [m for m in machines if m.get("id") != machine_id]
    with open(machines_file, "w") as f:
        json.dump(machines_data, f, indent=2)
        f.write("\n")
    print(f"  ✓ Removed '{machine_id}' from machines.json")

    # Step 4: Print manual steps
    print()
    print("=" * 50)
    print("MANUAL STEPS REQUIRED:")
    print("=" * 50)
    print()
    print("1. Remove SSH config entry:")
    print(f"   Edit ~/.ssh/config and remove the 'Host {machine_id}' block")
    print()
    print("2. Delete GitHub deploy keys:")
    print("   gh repo deploy-key list --repo <user>/<repo>")
    print(f"   # Find keys titled '{machine_id}' and delete them:")
    print("   gh repo deploy-key delete <key-id> --repo <user>/<repo>")
    print()
    print("3. Destroy remote machine:")
    print("   Option A: Delete user only")
    print("     ssh root@<ip> 'pkill -u agentwire; userdel -r agentwire'")
    print("   Option B: Destroy the VM entirely via provider console")
    print()
    print("4. Restart portal to pick up changes:")
    print("   agentwire portal stop && agentwire portal start")
    print()

    return 0


def cmd_machine_list(args) -> int:
    """List registered machines."""
    json_mode = getattr(args, 'json', False)
    machines_file = CONFIG_DIR / "machines.json"

    if not machines_file.exists():
        if json_mode:
            _output_json({"success": True, "machines": []})
        else:
            print("No machines registered.")
            print(f"  Config: {machines_file}")
        return 0

    try:
        with open(machines_file) as f:
            machines_data = json.load(f)
    except json.JSONDecodeError as e:
        if json_mode:
            _output_json({"success": False, "error": f"Invalid machines.json: {e}"})
        else:
            print(f"Invalid machines.json: {e}", file=sys.stderr)
        return 1

    machines = machines_data.get("machines", [])

    if not machines:
        if json_mode:
            _output_json({"success": True, "machines": []})
        else:
            print("No machines registered.")
        return 0

    # Enrich with tunnel status
    result_machines = []
    for m in machines:
        machine_id = m.get("id", "?")
        host = m.get("host", machine_id)
        user = m.get("user", "")
        projects_dir = m.get("projects_dir", "~")

        # Check if tunnel is running
        result = subprocess.run(
            ["pgrep", "-f", f"autossh.*{machine_id}"],
            capture_output=True,
        )
        has_tunnel = result.returncode == 0

        result_machines.append({
            "id": machine_id,
            "host": host,
            "user": user,
            "projects_dir": projects_dir,
            "status": "tunnel" if has_tunnel else "no tunnel",
        })

    if json_mode:
        _output_json({"success": True, "machines": result_machines})
    else:
        print(f"Registered machines ({len(machines)}):")
        print()
        for m in result_machines:
            tunnel_status = "✓ tunnel" if m["status"] == "tunnel" else "✗ no tunnel"
            print(f"  {m['id']}")
            print(f"    Host: {m['host']}")
            print(f"    Projects: {m['projects_dir']}")
            print(f"    Status: {tunnel_status}")
            print()

    return 0


# === Dev Command ===

def cmd_dev(args) -> int:
    """Start or attach to the AgentWire dev/agentwire session."""
    session_name = "agentwire"
    project_dir = get_source_dir()

    if tmux_session_exists(session_name):
        print(f"Dev session exists. Attaching to '{session_name}'...")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    if not project_dir.exists():
        print(f"Project directory not found: {project_dir}", file=sys.stderr)
        return 1

    # Dev session uses agentwire role by default, plus the soul personality
    role_names = inject_soul(["agentwire"], load_config(), no_soul=getattr(args, 'no_soul', False))
    roles, missing = load_roles(role_names, project_dir)
    if missing:
        print(f"Warning: Roles not found: {', '.join(missing)}", file=sys.stderr)
        roles = None

    # Use bypass session type for dev session (full permissions)
    agent_type = detect_default_agent_type()
    session_type_str = f"{agent_type}-bypass"

    # Build agent command
    agent = build_agent_command(session_type_str, roles)

    agent_cmd = agent.command

    # Create session with env injected at creation time so the initial
    # shell sees the vars (see _build_tmux_env_flags docstring).
    print(f"Creating dev session '{session_name}' in {project_dir}...")
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name, "-c", str(project_dir),
        *_build_tmux_env_flags(agent.env),
    ])

    # Start agent with agentwire config
    if agent_cmd:
        wait_for_shell_prompt(session_name)
        subprocess.run([
            "tmux", "send-keys", "-t", session_name, agent_cmd, "Enter",
        ])

    print("Attaching... (Ctrl+B D to detach)")
    subprocess.run(["tmux", "attach-session", "-t", session_name])
    return 0


# === Scratchpad Commands ===

def _ping_scratchpad_changed() -> None:
    """Best-effort: tell a running portal the pad changed so clients refresh."""
    portal_url = _get_portal_url()
    if not portal_url:
        return
    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        req = urllib.request.Request(
            f"{portal_url}/api/scratchpad/changed", data=b"{}",
            headers={"Content-Type": "application/json", **_portal_auth_headers()},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3, context=ctx)
    except Exception:
        pass  # portal down — file is still the source of truth


def cmd_scratchpad_list(args) -> int:
    """List scratch pad notes (newest first)."""
    from . import scratchpad
    json_mode = getattr(args, "json", False)
    notes = scratchpad.load_notes()
    if json_mode:
        _output_json({"success": True, "notes": notes})
        return 0
    if not notes:
        print("Scratch pad is empty.")
        return 0
    for n in notes:
        src = f" [{n['source']}]" if n.get("source") else ""
        first_line = n["text"].splitlines()[0][:80]
        more = " …" if ("\n" in n["text"] or len(n["text"]) > 80) else ""
        print(f"  {n['id']}{src}  {first_line}{more}")
    return 0


def cmd_scratchpad_add(args) -> int:
    """Add a note to the scratch pad."""
    from . import scratchpad
    json_mode = getattr(args, "json", False)
    try:
        note = scratchpad.add_note(args.text, source=getattr(args, "source", None))
    except ValueError as e:
        return _output_result(False, json_mode, str(e))
    _ping_scratchpad_changed()
    return _output_result(True, json_mode, f"Added note {note['id']}", note=note)


def cmd_scratchpad_remove(args) -> int:
    """Remove a note by id."""
    from . import scratchpad
    json_mode = getattr(args, "json", False)
    if not scratchpad.remove_note(args.id):
        return _output_result(False, json_mode, f"No note with id: {args.id}")
    _ping_scratchpad_changed()
    return _output_result(True, json_mode, f"Removed note {args.id}")


def cmd_scratchpad_clear(args) -> int:
    """Remove all notes."""
    from . import scratchpad
    json_mode = getattr(args, "json", False)
    count = scratchpad.clear_notes()
    _ping_scratchpad_changed()
    return _output_result(True, json_mode, f"Cleared {count} note(s)", count=count)


# === Services Commands ===

def _load_services_registry():
    """(config, registry) for the services commands."""
    from . import services as services_mod
    from .config import load_config as load_config_typed
    cfg = load_config_typed()
    return services_mod, services_mod.registry(cfg)


def _find_service(services_mod, reg, name: str):
    for svc in reg:
        if svc.name == name:
            return svc
    return None


def cmd_services_list(args) -> int:
    """List registered custom services (built-ins + config-defined)."""
    json_mode = getattr(args, "json", False)
    services_mod, reg = _load_services_registry()
    disabled = services_mod.load_disabled()

    entries = [{
        "name": svc.name,
        "project": svc.project,
        "autostart": svc.autostart,
        "restart": svc.restart,
        "healthcheck": {"kind": svc.healthcheck.kind, "interval": svc.healthcheck.interval},
        "roles": svc.roles,
        "type": svc.type,
        "disabled": svc.name in disabled,
    } for svc in reg]

    if json_mode:
        _output_json({"success": True, "services": entries})
        return 0

    if not entries:
        print("No custom services registered (services.custom in ~/.agentwire/config.yaml).")
        return 0
    for e in entries:
        flags = []
        if not e["autostart"]:
            flags.append("autostart off")
        if e["disabled"]:
            flags.append("disabled")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(f"  {e['name']}  restart={e['restart']}  "
              f"healthcheck={e['healthcheck']['kind']}/{e['healthcheck']['interval']}s{suffix}")
        if e["project"]:
            print(f"    project: {e['project']}")
    return 0


def cmd_services_status(args) -> int:
    """Health status for one or all custom services (runs healthchecks now).

    Exit 0 when everything that should be running is healthy, 1 otherwise.
    """
    json_mode = getattr(args, "json", False)
    name = getattr(args, "name", None)
    services_mod, reg = _load_services_registry()

    if name:
        svc = _find_service(services_mod, reg, name)
        if svc is None:
            return _output_result(False, json_mode, f"Unknown service: {name}")
        reg = [svc]

    statuses = [services_mod.service_status(svc) for svc in reg]
    # Disabled / autostart-off services aren't expected to be running
    all_ok = all(s["healthy"] or s["disabled"] or not s["autostart"] for s in statuses)

    if json_mode:
        # Always exit 0 in JSON mode — the payload carries all_healthy, and
        # callers (portal watchdog) need the data precisely when unhealthy.
        _output_json({"success": True, "all_healthy": all_ok, "services": statuses})
        return 0

    for s in statuses:
        if s["healthy"]:
            mark = "[ok]"
        elif s["disabled"] or not s["autostart"]:
            mark = "[..]"
        else:
            mark = "[!!]"
        extra = " (disabled)" if s["disabled"] else ("" if s["autostart"] else " (autostart off)")
        print(f"  {mark} {s['name']}: {s['detail']}{extra}")
    return 0 if all_ok else 1


def cmd_services_up(args) -> int:
    """Start a service (clears any 'down' state), or --all autostart services."""
    json_mode = getattr(args, "json", False)
    name = getattr(args, "name", None)
    services_mod, reg = _load_services_registry()

    if getattr(args, "all", False):
        from .config import load_config as load_config_typed
        results = services_mod.start_all_autostart(load_config_typed())
        ok = all(r.get("ok", True) for r in results)
        if json_mode:
            # Always exit 0 in JSON mode — per-service results carry the
            # failures; the portal autostart needs them either way.
            _output_json({"success": ok, "results": results})
            return 0
        for r in results:
            if "skipped" in r:
                print(f"  [..] {r['name']}: {r['skipped']}")
            else:
                print(f"  [{'ok' if r['ok'] else '!!'}] {r['name']}: {r['result']}")
        return 0 if ok else 1

    if not name:
        return _output_result(False, json_mode, "Service name required (or --all)")
    svc = _find_service(services_mod, reg, name)
    if svc is None:
        return _output_result(False, json_mode, f"Unknown service: {name}")

    services_mod.set_disabled(name, False)
    ok, msg = services_mod.start_service(svc)
    return _output_result(ok, json_mode, f"{name}: {msg}", name=name, result=msg)


def cmd_services_down(args) -> int:
    """Stop a service and keep it stopped (watchdog and up --all skip it)."""
    json_mode = getattr(args, "json", False)
    name = args.name
    services_mod, reg = _load_services_registry()
    if _find_service(services_mod, reg, name) is None:
        return _output_result(False, json_mode, f"Unknown service: {name}")

    # Disable BEFORE killing so the watchdog can't race a respawn
    services_mod.set_disabled(name, True)
    ok, msg = services_mod.stop_service(name)
    return _output_result(ok, json_mode, f"{name}: {msg} (disabled until 'services up {name}')",
                          name=name, result=msg, disabled=True)


def cmd_up(args) -> int:
    """Boot all AgentWire services, then start/attach the dev session.

    Brings up (detached): portal, TTS, STT, and any autostart custom
    services from config. The scheduler is auto-started by the portal
    (services.scheduler.autostart). Then runs `agentwire dev` to
    create + attach the main session.

    Per-service start is best-effort — a failure to start one service
    doesn't block the rest or the dev session.
    """
    from argparse import Namespace

    from .config import load_config as load_config_typed
    from .network import NetworkContext

    cfg_dict = load_config()
    cfg = load_config_typed()
    ctx = NetworkContext.from_config()

    def _is_local(name: str) -> bool:
        try:
            return ctx.is_local(name)
        except Exception:
            return True

    print("Bringing up AgentWire services...")

    # Portal (autostarts the scheduler on boot)
    if _is_local("portal"):
        portal_args = Namespace(
            dev=getattr(args, "dev", False),
            no_tts=getattr(args, "no_tts", False),
            no_stt=getattr(args, "no_stt", False),
            port=None, host=None,
            config=getattr(args, "config", None),
        )
        _start_portal_local(portal_args, attach=False)
    else:
        print("  Portal configured for a remote machine — skipping (start it there).")

    # TTS — only the custom tier has a local service to start
    tts_backend = cfg_dict.get("tts", {}).get("backend", "default")
    if getattr(args, "no_tts", False):
        print("  TTS skipped (--no-tts).")
    elif tts_backend != "custom":
        print("  TTS skipped (default tier — browser/OS voice, no service needed).")
    elif _is_local("tts"):
        from . import tts_cli
        tts_args = Namespace(port=None, host=None, backend=None)
        tts_cli._start_tts_local(tts_args, attach=False)
    else:
        print("  TTS configured for a remote machine — skipping (start it there).")

    # STT — only the custom tier has a local service to start
    if getattr(args, "no_stt", False):
        print("  STT skipped (--no-stt).")
    elif cfg_dict.get("stt", {}).get("backend", "default") == "custom":
        from . import tts_cli
        stt_args = Namespace(port=None, host=None, model=None, backend=None)
        tts_cli.cmd_stt_start(stt_args)
    else:
        print("  STT skipped (default tier — portal-owned Moonshine, "
              "auto-downloads on first boot; no service needed).")

    # Custom services (same shared path as portal-launch autostart)
    from . import services as services_mod
    print("Starting custom services...")
    for r in services_mod.start_all_autostart(cfg):
        if "skipped" in r:
            print(f"  [..] {r['name']}: {r['skipped']}")
        elif r.get("ok"):
            print(f"  [ok] {r['name']} ({r['result']})")
        else:
            print(f"  [!!] {r['name']}: {r['result']}", file=sys.stderr)

    print()
    # Finally, the dev session (creates + attaches the agentwire session)
    return cmd_dev(args)


# === Init Command ===

def cmd_init(args) -> int:
    """Initialize AgentWire configuration with interactive wizard.

    Default behavior: Run the wizard and end on the concrete portal-URL next
    steps, so a first-run evaluator lands on a working voice portal.
    Assisted mode (--assisted): also spawn the interactive Claude setup
    session at the end to configure TTS/STT and other services.
    """
    # Check Python version first
    if not check_python_version():
        return 1

    # Check for externally-managed environment (Ubuntu)
    if not check_pip_environment():
        print("Please set up a virtual environment before running init.")
        return 1

    from .onboarding import run_onboarding

    # Default ends on the portal-URL next steps; --assisted opts into the
    # interactive Claude setup session.
    return run_onboarding(skip_session=not args.assisted)


def cmd_generate_certs(args) -> int:
    """Generate SSL certificates."""
    return generate_certs()


# === Listen Commands ===

def cmd_listen_start(args) -> int:
    """Start voice recording."""
    from .listen import start_recording
    return start_recording()


def cmd_listen_stop(args) -> int:
    """Stop recording, transcribe, send to session or type at cursor."""
    from .listen import stop_recording
    session = args.session or "agentwire"
    type_at_cursor = getattr(args, 'type', False)
    transcribe_only = getattr(args, 'stdout', False)
    return stop_recording(session, voice_prompt=not args.no_prompt,
                          type_at_cursor=type_at_cursor, transcribe_only=transcribe_only)


def cmd_listen_cancel(args) -> int:
    """Cancel current recording."""
    from .listen import cancel_recording
    return cancel_recording()


def cmd_listen_toggle(args) -> int:
    """Toggle recording (start if not recording, stop if recording)."""
    from .listen import is_recording, start_recording, stop_recording
    session = args.session or "agentwire"
    if is_recording():
        return stop_recording(session, voice_prompt=not args.no_prompt)
    else:
        return start_recording()


# === Network Commands ===


def cmd_network_status(args) -> int:
    """Show complete network health at a glance."""
    from .network import NetworkContext
    from .tunnels import TunnelManager, test_service_health, test_ssh_connectivity

    ctx = NetworkContext.from_config()
    tm = TunnelManager()
    issues = []

    # Print header
    print("AgentWire Network Status")
    print("=" * 60)
    hostname = ctx.local_machine_id or socket.gethostname()
    print(f"\nYou are on: {hostname}")

    # Check machines (SSH connectivity)
    print("\nMachines")
    print("-" * 60)

    for machine_id, machine in ctx.machines.items():
        is_local = machine_id == ctx.local_machine_id
        host = machine.get("host", machine_id)
        user = machine.get("user")

        if is_local:
            print(f"  {machine_id:<16}(this machine)    [ok] reachable")
        else:
            latency = test_ssh_connectivity(host, user, timeout=5)
            if latency is not None:
                print(f"  {machine_id:<16}{host:<18}[ok] reachable (ssh: {latency}ms)")
            else:
                print(f"  {machine_id:<16}{host:<18}[!!] unreachable")
                issues.append({
                    "type": "machine_unreachable",
                    "machine": machine_id,
                    "host": host,
                })

    # Check services
    print("\nServices")
    print("-" * 60)

    # Resolve the ACTIVE voice tier for each subsystem and only health-check a
    # server when that tier actually uses one. A tier with no server (default
    # TTS, cloud/browser-fallback STT) reports its path, not a phantom probe —
    # and an orphaned engine server (up but unused by the tier) is flagged.
    from .config import load_config as load_config_typed
    from .voice_status import resolve_stt_status, resolve_tts_status

    typed_cfg = load_config_typed()
    for resolver in (resolve_tts_status, resolve_stt_status):
        vs = resolver(typed_cfg)
        label = vs.subsystem.upper()
        loc = vs.server_url if vs.server_url else f"{vs.tier} tier"
        if vs.ready:
            mark, note = "[ok]", (vs.path if not vs.server_url else "running")
        else:
            mark, note = "[!!]", vs.detail
            issues.append({"type": "service_down", "service": vs.subsystem,
                            "location": loc, "error": vs.detail})
        print(f"  {label:<16}{loc:<18}{mark} {note}")
        for w in vs.warnings:
            print(f"  {'':<16}{'':<18}[..] {w}")
            issues.append({"type": "voice_orphan", "service": vs.subsystem, "warning": w})

    for service_name in ["portal"]:
        service_config = getattr(ctx.config.services, service_name, None)
        if service_config is None:
            continue

        if ctx.is_local(service_name):
            location = f"localhost:{service_config.port}"
            via = "(local)"
        else:
            machine = service_config.machine
            location = f"{machine}:{service_config.port}"
            via = "(via tunnel)"

        # Test the service health
        url = ctx.get_service_url(service_name)
        health_url = f"{url}{service_config.health_endpoint}"
        is_healthy, error = test_service_health(health_url, timeout=3)

        if is_healthy:
            print(f"  {service_name.capitalize():<16}{location:<18}[ok] running {via}")
        else:
            print(f"  {service_name.capitalize():<16}{location:<18}[!!] not responding")
            issues.append({
                "type": "service_down",
                "service": service_name,
                "location": location,
                "error": error,
            })

    # Check tunnels
    required_tunnels = ctx.get_required_tunnels()
    if required_tunnels:
        print("\nTunnels (this machine)")
        print("-" * 60)

        for spec in required_tunnels:
            status = tm.check_tunnel(spec)
            target = f"localhost:{spec.local_port}"

            if status.status == "up":
                print(f"  -> {spec.remote_machine:<12}{target:<18}[ok] up (PID {status.pid})")
            else:
                print(f"  -> {spec.remote_machine:<12}{target:<18}[!!] down")
                issues.append({
                    "type": "tunnel_down",
                    "spec": spec,
                    "error": status.error,
                })

    # Check for worker sessions
    print("\nWorker Sessions")
    print("-" * 60)

    # Local sessions
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        sessions = [s for s in result.stdout.strip().split("\n") if s and not s.startswith("agentwire")]
        if sessions:
            print(f"  {hostname:<16}{len(sessions)} sessions    {', '.join(sessions[:5])}")
            if len(sessions) > 5:
                print(f"  {'':<16}... and {len(sessions) - 5} more")
        else:
            print(f"  {hostname:<16}0 sessions")
    else:
        print(f"  {hostname:<16}(no tmux server)")

    # Remote sessions
    for machine_id, machine in ctx.machines.items():
        if machine_id == ctx.local_machine_id:
            continue

        result = _run_remote(machine_id, "tmux list-sessions -F '#{session_name}' 2>/dev/null")
        if result.returncode == 0 and result.stdout.strip():
            sessions = [s for s in result.stdout.strip().split("\n") if s]
            if sessions:
                print(f"  {machine_id:<16}{len(sessions)} sessions    {', '.join(sessions[:5])}")
                if len(sessions) > 5:
                    print(f"  {'':<16}... and {len(sessions) - 5} more")
        else:
            print(f"  {machine_id:<16}0 sessions")

    # Summary
    print()
    if not issues:
        print("Everything looks good!")
    else:
        print(f"Issues detected: {len(issues)}")
        print()
        for i, issue in enumerate(issues, 1):
            if issue["type"] == "machine_unreachable":
                print(f"  {i}. Machine '{issue['machine']}' unreachable")
                print(f"     Host: {issue['host']}")
                print()
                print("     To fix:")
                print(f"       Check SSH connectivity: ssh {issue['host']}")
                print("       Verify machine is running")
                print()

            elif issue["type"] == "service_down":
                print(f"  {i}. {issue['service'].capitalize()} not responding")
                print(f"     Location: {issue['location']}")
                if issue.get("error"):
                    print(f"     Error: {issue['error']}")
                print()
                print("     To fix:")
                if issue["service"] == "portal":
                    print("       agentwire portal start")
                elif issue["service"] == "tts":
                    print("       agentwire tts start")
                print("       agentwire tunnels check  # Verify tunnel health")
                print()

            elif issue["type"] == "tunnel_down":
                spec = issue["spec"]
                print(f"  {i}. Missing tunnel")
                print(f"     Required: localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port}")
                if issue.get("error"):
                    print(f"     Error: {issue['error']}")
                print()
                print("     To fix:")
                print("       agentwire tunnels up")
                print()

        print("-" * 60)
        print()
        print("Run: agentwire doctor    # Auto-fix common issues")

    return 0 if not issues else 1


def cmd_safety_check(args) -> int:
    """CLI command: agentwire safety check"""
    command = args.command
    verbose = getattr(args, 'verbose', False)
    return cli_safety.safety_check_cmd(command, verbose)


def cmd_safety_status(args) -> int:
    """CLI command: agentwire safety status"""
    return cli_safety.safety_status_cmd()


def cmd_safety_notify_unattended_block(args) -> int:
    """CLI command: agentwire safety notify-unattended-block (hook-invoked)"""
    return cli_safety.safety_notify_unattended_block_cmd(
        getattr(args, "reason", "") or "",
        getattr(args, "rule_id", "") or "",
        getattr(args, "command", "") or "",
    )


def cmd_safety_logs(args) -> int:
    """CLI command: agentwire safety logs"""
    tail = getattr(args, 'tail', None)
    session = getattr(args, 'session', None)
    today = getattr(args, 'today', False)
    pattern = getattr(args, 'pattern', None)
    return cli_safety.safety_logs_cmd(tail, session, today, pattern)


def cmd_safety_install(args) -> int:
    """CLI command: agentwire safety install"""
    return cli_safety.safety_install_cmd(assume_yes=getattr(args, "yes", False))


def cmd_safety_tooldefs_list(args) -> int:
    """CLI command: agentwire safety tooldefs list"""
    return cli_safety.safety_tooldefs_list_cmd()


def cmd_safety_tooldefs_show(args) -> int:
    """CLI command: agentwire safety tooldefs show <tool>"""
    return cli_safety.safety_tooldefs_show_cmd(args.tool)


def _render_skill_section() -> int:
    """Print the global-skill drift block. Returns the count of issues found.

    Hand-placed at wiki-setup and never resynced, so a stale or missing copy was
    invisible until #475. Flagged the same way as hooks. `source-unavailable`
    (running from a checkout, where skills only live in the built wheel) is NOT a
    drift problem — there's nothing to install from — so it never bumps the count.
    """
    issues = 0
    for name, state in sorted(skill_drift().items()):
        target = CLAUDE_SKILLS_DIR / name
        if state == "ok":
            print(f"  [ok] /{name} skill: {target}")
        elif state == "source-unavailable":
            print(f"  [..] /{name} skill: source not packaged here (running from a checkout)")
        elif state == "stale":
            print(f"  [!!] /{name} skill: STALE — installed copy differs from packaged source")
            print("     Run: agentwire hooks install")
            issues += 1
        else:
            print(f"  [!!] /{name} skill: not installed")
            print("     Run: agentwire hooks install")
            issues += 1
    return issues


def _render_damage_control_section() -> int:
    """Print the damage-control health block. Returns the count of issues found.

    Covers the four #462 blind spots plus DC hook staleness: the global kill
    switch (``safety.enabled: false``), installed-rules drift vs the bundled
    rules, PreToolUse matcher registration, and DC hook-script staleness.
    """
    issues = 0

    # Kill switch — ``enabled`` in ~/.agentwire/damagecontrol.yml gates ALL
    # damage control. A silent `false` is the loudest possible failure, so flag
    # it first and hard.
    try:
        from .safety._core import load_safety_config
        safety_enabled = load_safety_config().get("enabled", True)
    except Exception as e:
        print(f"  [..] Could not read safety config: {e}")
        safety_enabled = True
    if not safety_enabled:
        print("  [!!] Damage control is DISABLED (enabled: false)")
        print("       ALL command/path/outbound gating is off.")
        print("       Fix: set enabled: true in ~/.agentwire/damagecontrol.yml")
        issues += 1
    else:
        print("  [ok] Damage control enabled (enabled: true)")

    try:
        from . import cli_safety
    except Exception as e:
        print(f"  [..] Could not load safety module: {e}")
        return issues

    # DC hook-script staleness (bash/edit/write/mcp-tool + audit_logger).
    hook_drift = cli_safety.damage_control_hook_drift()
    stale_hooks = [f for f, s in hook_drift.items() if s == "stale"]
    missing_hooks = [f for f, s in hook_drift.items() if s == "missing"]
    if stale_hooks or missing_hooks:
        if missing_hooks:
            print(f"  [!!] DC hook scripts missing: {', '.join(sorted(missing_hooks))}")
        if stale_hooks:
            print(f"  [!!] DC hook scripts STALE: {', '.join(sorted(stale_hooks))}")
        print("       Fix: agentwire safety install --yes")
        issues += 1
    else:
        print("  [ok] DC hook scripts current")

    # Installed-rules drift vs bundled rules (the incident's missing files).
    rule_drift = cli_safety.rules_drift()
    missing_rules = [f for f, s in rule_drift.items() if s == "missing"]
    stale_rules = [f for f, s in rule_drift.items() if s == "stale"]
    if missing_rules:
        print(f"  [!!] Damage-control rules NOT installed: {', '.join(sorted(missing_rules))}")
        print("       Fix: agentwire safety install --yes")
        issues += 1
    elif stale_rules:
        # Stale = installed copy differs from bundled. Could be an intentional
        # customization, so warn (not error) and don't auto-overwrite.
        print(f"  [..] Damage-control rules differ from bundled: {', '.join(sorted(stale_rules))}")
        print("       (customized? `agentwire safety install --yes` leaves these untouched)")
    else:
        print("  [ok] Damage-control rules installed and match bundled")

    # Matcher presence in ~/.claude/settings.json.
    missing_matchers = cli_safety.missing_damage_control_matchers()
    if missing_matchers:
        print(f"  [!!] PreToolUse matchers not registered: {', '.join(missing_matchers)}")
        print("       Fix: agentwire safety install --yes")
        issues += 1
    else:
        print("  [ok] PreToolUse damage-control matchers registered")

    return issues


def _render_voice_loop_section(config, ctx) -> int:
    """Print the voice-loop (push-to-talk) preflight. Returns failures found.

    Read-only pass/fail per stage with an actionable fix line when red — the
    existing infra sections keep the auto-fix prompts.
    """
    from .doctor_voice import run_voice_loop_checks

    print("\nChecking voice loop (push-to-talk)...")
    failures = 0
    for stage in run_voice_loop_checks(config, ctx):
        if stage.status == "ok":
            print(f"  [ok] {stage.name}: {stage.detail}")
        elif stage.status == "info":
            print(f"  [..] {stage.name}: {stage.detail}")
        else:
            print(f"  [!!] {stage.name}: {stage.detail}")
            for fix in stage.fixes:
                print(f"       Fix: {fix}")
            failures += 1
    return failures


def cmd_doctor(args) -> int:
    """Auto-diagnose and fix common issues."""
    from .network import NetworkContext
    from .tunnels import TunnelManager, test_service_health, test_ssh_connectivity
    from .validation import validate_config

    dry_run = getattr(args, 'dry_run', False)
    auto_confirm = getattr(args, 'yes', False)
    voice_only = getattr(args, 'voice', False)

    print("AgentWire Doctor")
    print("=" * 60)

    issues_found = 0
    issues_fixed = 0

    # --voice: run ONLY the end-to-end push-to-talk preflight (fast, no SSH
    # waits) — for tight first-run / break-a-dependency verification loops.
    if voice_only:
        try:
            from .config import load_config as load_config_typed
            config = load_config_typed()
        except Exception as e:
            print(f"\n  [!!] Config file error: {e}")
            print("     Run: agentwire init")
            return 1
        try:
            ctx = NetworkContext.from_config()
        except Exception:
            ctx = None
        issues_found = _render_voice_loop_section(config, ctx)
        print()
        print("-" * 60)
        print("Voice loop ready!" if issues_found == 0
              else f"Voice loop: {issues_found} stage(s) need attention")
        return 0 if issues_found == 0 else 1

    # 1. Check Python version
    print("\nChecking Python version...")
    py_version = sys.version_info
    version_str = f"{py_version.major}.{py_version.minor}.{py_version.micro}"
    if py_version >= (3, 10):
        print(f"  [ok] Python {version_str} (>=3.10 required)")
    else:
        print(f"  [!!] Python {version_str} (>=3.10 required)")
        print("     macOS: pyenv install 3.12.0 && pyenv global 3.12.0")
        print("     Ubuntu: sudo apt update && sudo apt install python3.12")
        issues_found += 1

    # 2. Check system dependencies
    print("\nChecking system dependencies...")

    # Check tmux (required)
    tmux_path = shutil.which("tmux")
    if tmux_path:
        print(f"  [ok] tmux: {tmux_path}")
        # Server-side options that make or break the agent UX (warn-only —
        # config is user preference, not a broken install). Only checkable
        # when a tmux server is running.
        for opt, why in (
            ("focus-events", "Claude Code shows a setup tip on every session start"),
            ("mouse", "no mouse scroll or text selection in agent panes"),
        ):
            val = _tmux_global_option(opt)
            if val is None:
                break  # no running tmux server — nothing to inspect
            if val == "on":
                print(f"  [ok] tmux {opt}: on")
            else:
                print(f"  [..] tmux {opt}: {val} — {why}")
                print("     Recommended config: agentwire init (tmux step), or see")
                print("     docs/wiki/quickstart.md#recommended-tmux-config")
    else:
        print("  [!!] tmux: not found (required)")
        print("     macOS: brew install tmux")
        print("     Ubuntu: sudo apt install tmux")
        issues_found += 1

    # Check ffmpeg (optional)
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        print(f"  [ok] ffmpeg: {ffmpeg_path}")
    else:
        print("  [..] ffmpeg: not found (optional, needed for voice input)")
        print("     macOS: brew install ffmpeg")
        print("     Ubuntu: sudo apt install ffmpeg")

    # Check Claude Code (optional)
    claude_path = shutil.which("claude")
    if claude_path:
        print(f"  [ok] claude: {claude_path}")
    else:
        print("  [..] claude: not found (optional, use --bare sessions or other agents)")
        print("     Install: https://github.com/anthropics/claude-code")

    # Check Pi coding agent (optional, for pi-* session types)
    pi_path = shutil.which("pi")
    if pi_path:
        try:
            # Pi prints --version to stderr, so merge with stdout
            result = subprocess.run(
                [pi_path, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            pi_version = (result.stdout + result.stderr).strip()
            print(f"  [ok] pi: {pi_path} (v{pi_version})")
        except Exception:
            print(f"  [ok] pi: {pi_path}")
    else:
        print("  [..] pi: not found (optional, required for pi-* session types)")
        print("     Install: npm install -g @mariozechner/pi-coding-agent")

    # 3. Check AgentWire scripts
    print("\nChecking AgentWire scripts...")

    say_path = shutil.which("say")
    if say_path:
        print(f"  [ok] say: {say_path}")
    else:
        print("  [..] say: not found (optional, use 'agentwire say' directly)")

    # 4. Check agentwire-owned hook files (existence AND drift from packaged source)
    print("\nChecking AgentWire hooks...")

    hook_meta = {
        "agentwire-permission.sh": ("Permission hook", False,
                                    "optional for prompted sessions"),
        "idle-handler.sh": ("Idle notification hook", True,
                            "required for worker notifications and scheduled tasks"),
        "queue-processor.sh": ("Queue processor", True,
                               "required for notification queuing"),
    }
    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        hooks_source = None

    for hook_name, target_dir, _event in _managed_hook_files():
        label, required, why = hook_meta[hook_name]
        target = target_dir / hook_name
        source = hooks_source / hook_name if hooks_source else None
        state = _managed_file_state(target, source) if source and source.exists() \
            else ("ok" if target.exists() else "missing")

        if state == "ok":
            print(f"  [ok] {label}: {target}")
        elif state == "stale":
            print(f"  [!!] {label}: STALE — installed copy differs from packaged source")
            print("     Run: agentwire hooks install")
            issues_found += 1
        elif required:
            print(f"  [!!] {label}: not found ({why})")
            print("     Run: agentwire hooks install")
            issues_found += 1
        else:
            print(f"  [..] {label}: not found ({why})")
            print("     Run: agentwire hooks install")

    # 4a-bis. Global skills (currently just /wiki). Hand-placed at wiki-setup and
    # never resynced, so a stale or missing copy was invisible until #475. Flag
    # the same way as hooks — drift-aware against the packaged source.
    print("\nChecking AgentWire global skills...")
    issues_found += _render_skill_section()

    # 4b. Damage control (safety) — the kill switch, install drift, and the
    # PreToolUse matcher registration. The #462 incident: a global disable and
    # missing rule files were both invisible to every diagnostic.
    print("\nChecking damage control (safety)...")
    issues_found += _render_damage_control_section()

    # 4c. Local checkout vs origin/main — rebuild reinstalls whatever is checked
    # out, so a never-pulled main silently ships stale code. Today only worktree
    # creation fetches; surface the drift here too.
    print("\nChecking source checkout...")
    src_root = Path(__file__).parent.parent
    if not (src_root / "pyproject.toml").exists():
        try:
            src_root = get_source_dir()
        except Exception:
            src_root = None
    if src_root is None:
        print("  [..] Could not locate source checkout — skipping git-drift check")
    else:
        behind, err = _git_behind_origin(src_root)
        if err:
            print(f"  [..] Source checkout: skipped git-drift check ({err})")
        elif behind and behind > 0:
            print(f"  [!!] Local main is {behind} commit(s) behind origin/main")
            print(f"       {src_root}")
            print("       Fix: git pull --ff-only  (then agentwire rebuild)")
            issues_found += 1
        else:
            print("  [ok] Local main up to date with origin/main")

    # Check custom services (registry-driven: built-in notifications bridge
    # + user-defined services from services.custom)
    print("\nChecking custom services...")
    from . import services as services_mod
    from .config import load_config as _load_config_typed
    try:
        _svc_cfg = _load_config_typed()
        _svc_disabled = services_mod.load_disabled()
        for svc in services_mod.registry(_svc_cfg):
            healthy, detail = services_mod.run_healthcheck(svc)
            if healthy:
                print(f"  [ok] Service {svc.name}: {detail}")
            elif svc.name in _svc_disabled:
                print(f"  [..] Service {svc.name}: stopped via 'services down' ({detail})")
            elif not svc.autostart:
                print(f"  [..] Service {svc.name}: not running (autostart off, {detail})")
            else:
                print(f"  [!!] Service {svc.name}: unhealthy — {detail}")
                print(f"     Run: agentwire services up {svc.name}")
                issues_found += 1
    except Exception as e:
        print(f"  [..] Could not check custom services: {e}")

    # 5. Validate config
    print("\nChecking configuration...")
    try:
        from .config import load_config as load_config_typed
        config = load_config_typed()
        print("  [ok] Config file valid")
    except Exception as e:
        print(f"  [!!] Config file error: {e}")
        print("     Run: agentwire init")
        issues_found += 1
        return 1  # Can't proceed without valid config

    machines_file = config.machines.file
    warnings, errors = validate_config(config, machines_file)

    if not errors:
        print("  [ok] Machines.json valid")
    else:
        for err in errors:
            print(f"  [!!] {err.message}")
            issues_found += 1

    if not warnings:
        print("  [ok] All config checks passed")
    else:
        for warn in warnings:
            print(f"  [..] {warn.message}")

    # 6. Check SSH connectivity
    print("\nChecking SSH connectivity...")
    ctx = NetworkContext.from_config()

    for machine_id, machine in ctx.machines.items():
        if machine_id == ctx.local_machine_id:
            continue

        host = machine.get("host", machine_id)
        user = machine.get("user")

        latency = test_ssh_connectivity(host, user, timeout=5)
        if latency is not None:
            print(f"  [ok] {machine_id}: reachable ({latency}ms)")
        else:
            print(f"  [!!] {machine_id}: unreachable")
            issues_found += 1

    # 7. Check/create tunnels
    print("\nChecking tunnels...")
    tm = TunnelManager()
    required_tunnels = ctx.get_required_tunnels()

    if not required_tunnels:
        print("  [ok] No tunnels required (services are local)")
    else:
        for spec in required_tunnels:
            status = tm.check_tunnel(spec)

            if status.status == "up":
                print(f"  [ok] localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port} (PID {status.pid})")
            else:
                print(f"  [!!] Missing: localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port}")
                issues_found += 1

                if not dry_run:
                    if auto_confirm or _confirm("     Create tunnel?"):
                        print("     -> Creating tunnel...", end=" ", flush=True)
                        result = tm.create_tunnel(spec, ctx)
                        if result.status == "up":
                            print(f"[ok] created (PID {result.pid})")
                            issues_fixed += 1
                        else:
                            print(f"[!!] failed: {result.error}")
                else:
                    print("     -> Would create tunnel (dry-run)")

    # 8. Check services
    print("\nChecking services...")

    # Default-tier TTS has no service to health-check
    tts_config = load_config().get("tts", {})
    tts_backend = tts_config.get("backend", "default")

    for service_name in ["portal", "tts"]:
        if service_name == "tts" and tts_backend != "custom":
            print("  [ok] Tts: default tier (browser/OS voice, no local service needed)")
            continue

        service_config = getattr(ctx.config.services, service_name, None)
        if service_config is None:
            continue

        url = ctx.get_service_url(service_name)
        health_url = f"{url}{service_config.health_endpoint}"
        is_healthy, error = test_service_health(health_url, timeout=3)

        if is_healthy:
            print(f"  [ok] {service_name.capitalize()}: responding on {url}")
        else:
            print(f"  [!!] {service_name.capitalize()}: not responding on {url}")
            if error:
                print(f"       Error: {error}")
            issues_found += 1

            # Only try to fix if service is local
            if ctx.is_local(service_name):
                if not dry_run:
                    if auto_confirm or _confirm(f"     Start {service_name}?"):
                        print(f"     -> Starting {service_name}...", end=" ", flush=True)

                        if service_name == "portal":
                            session_name = get_portal_session_name()
                            if tmux_session_exists(session_name):
                                print("[ok] already running in tmux")
                            else:
                                subprocess.run(
                                    ["tmux", "new-session", "-d", "-s", session_name],
                                    capture_output=True,
                                )
                                subprocess.run(
                                    ["tmux", "send-keys", "-t", session_name, "agentwire portal serve", "Enter"],
                                    capture_output=True,
                                )
                                print("[ok] started")
                                issues_fixed += 1

                        elif service_name == "tts":
                            session_name = get_tts_session_name()
                            if tmux_session_exists(session_name):
                                print("[ok] already running in tmux")
                            else:
                                subprocess.run(
                                    ["tmux", "new-session", "-d", "-s", session_name],
                                    capture_output=True,
                                )
                                subprocess.run(
                                    ["tmux", "send-keys", "-t", session_name, "agentwire tts serve", "Enter"],
                                    capture_output=True,
                                )
                                print("[ok] started")
                                issues_fixed += 1
                else:
                    print(f"     -> Would start {service_name} (dry-run)")
            else:
                print(f"     -> Service is remote, start it on {service_config.machine}")

    # 8b. Voice loop (push-to-talk) — walk the live PTT path end to end and
    # report each stage pass/fail with a fix line when red: mic/audio capture,
    # the STT shim (incl. the default-tier Moonshine :8101 shim, invisible to
    # the services loop), portal/tunnel reachability, and tmux+PTT wiring. This
    # subsumes the old standalone STT probe — the STT stage covers all tiers.
    issues_found += _render_voice_loop_section(config, ctx)

    # 8c. Secrets — for each configured feature that needs a key, report
    # whether its env var is present. Names only, never values. Keys live in
    # ~/.agentwire/.env (docs/wiki/security/secrets.md), loaded on every
    # entry point, so os.environ is the authoritative view here.
    raw_cfg = load_config()
    expected_keys: list[tuple[str, list[str]]] = []
    channels_cfg = raw_cfg.get("channels", {}) or {}
    if channels_cfg.get("email"):
        expected_keys.append(("channels.email (Resend)", ["RESEND_API_KEY"]))
    if channels_cfg.get("quo"):
        expected_keys.append(
            ("channels.quo (OpenPhone)", ["QUO_API_KEY", "OPENPHONE_API_KEY"])
        )
    for pname, pcfg in (raw_cfg.get("pi", {}).get("providers", {}) or {}).items():
        p_env_var = (pcfg or {}).get(
            "env_var", f"{pname.upper().replace('-', '_')}_API_KEY"
        )
        expected_keys.append((f"pi.providers.{pname}", [p_env_var]))
    if expected_keys:
        print("\nChecking secrets (~/.agentwire/.env)...")
        for feature, candidates in expected_keys:
            found = next((v for v in candidates if os.environ.get(v)), None)
            if found:
                print(f"  [ok] {feature}: {found} is set")
            else:
                print(f"  [!!] {feature}: {' / '.join(candidates)} not set")
                print(f"       Fix: add {candidates[0]}=... to ~/.agentwire/.env")
                issues_found += 1

    # 9. Validate remote machines
    print("\nChecking remote machines...")
    remote_machines = {mid: m for mid, m in ctx.machines.items() if mid != ctx.local_machine_id}

    if not remote_machines:
        print("  [ok] No remote machines configured")
    else:
        for machine_id, machine in remote_machines.items():
            host = machine.get("host", machine_id)
            user = machine.get("user")
            target = f"{user}@{host}" if user else host

            print(f"\n  {machine_id}:")

            # Check SSH connectivity (already done above, but include latency here)
            latency = test_ssh_connectivity(host, user, timeout=5)
            if latency is not None:
                print(f"    [ok] SSH connectivity ({latency}ms)")
            else:
                print("    [!!] SSH connectivity failed")
                print(f"         Fix: ssh {target}")
                issues_found += 1
                continue  # Can't check further if SSH fails

            # Check if agentwire is installed
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, "agentwire --version"],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                if result.returncode == 0:
                    version = result.stdout.strip()
                    print(f"    [ok] agentwire installed ({version})")
                else:
                    print("    [!!] agentwire not installed")
                    print(f"         Fix: ssh {target} 'pip install agentwire-dev'")
                    issues_found += 1
                    continue
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                print("    [!!] agentwire not installed")
                print(f"         Fix: ssh {target} 'pip install agentwire-dev'")
                issues_found += 1
                continue

            # Check portal_url file
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, "cat ~/.agentwire/portal_url"],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                if result.returncode == 0:
                    portal_url = result.stdout.strip()
                    print(f"    [ok] portal_url set ({portal_url})")
                else:
                    print("    [!!] portal_url not set")
                    print(f"         Fix: ssh {target} 'echo \"https://localhost:8765\" > ~/.agentwire/portal_url'")
                    issues_found += 1
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                print("    [!!] portal_url not set")
                print(f"         Fix: ssh {target} 'echo \"https://localhost:8765\" > ~/.agentwire/portal_url'")
                issues_found += 1

            # Test say command (optional)
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, "which say"],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                if result.returncode == 0:
                    print("    [ok] say command available")
                else:
                    print("    [..] say: not found (optional, use 'agentwire say' directly)")
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                print("    [..] say: not found (optional, use 'agentwire say' directly)")

    # 10. Check for dead-lettered messages
    print("\nChecking for dead-lettered messages...")
    from agentwire import inbox
    try:
        dead_sess = inbox.dead_sessions()
        done_or_esc_found = False
        if not dead_sess:
            print("  [ok] No dead-lettered messages found")
        else:
            for ds in dead_sess:
                dead_msgs = inbox.list_dead(ds)
                done_or_esc = [m for m in dead_msgs if m.kind in ("done", "escalation")]
                if done_or_esc:
                    done_or_esc_found = True
                    print(f"  [!!] Session '{ds}' has {len(done_or_esc)} dead-lettered report-back/escalation message(s):")
                    for m in done_or_esc:
                        print(f"       - Kind: {m.kind}, Sender: {m.sender}, Text: {m.text}, Reason: {m.reason}")
                    issues_found += 1
            if not done_or_esc_found:
                print("  [ok] No dead-lettered done/escalation messages found (any dead-letters are informational)")
    except Exception as e:
        print(f"  [..] Could not check dead-lettered messages: {e}")

    # Summary
    print()
    print("-" * 60)
    if issues_found == 0:
        print("All checks passed!")
    elif issues_fixed == issues_found:
        print(f"All issues resolved! ({issues_fixed} fixed)")
    elif issues_fixed > 0:
        print(f"Fixed {issues_fixed} of {issues_found} issues")
    else:
        print(f"Found {issues_found} issues")

    return 0 if issues_found == issues_fixed else 1


def _confirm(prompt: str) -> bool:
    """Ask for user confirmation."""
    try:
        response = input(f"{prompt} [y/N] ").strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# === Rebuild/Uninstall Commands ===

UV_CACHE_DIR = Path.home() / ".cache" / "uv"


def cmd_rebuild(args) -> int:
    """Rebuild: clear uv cache, uninstall, reinstall from source.

    This is the correct way to pick up source changes when developing.
    `uv tool install . --force` does NOT work - it uses cached wheels.
    """
    force = getattr(args, "force", False)

    print("Rebuilding agentwire-dev...")
    print()

    # Resolve the source checkout up front so the git-drift guard and the
    # install step agree on which tree they're operating over.
    project_root = Path(__file__).parent.parent
    if not (project_root / "pyproject.toml").exists():
        project_root = get_source_dir()

    # Git-drift guard: rebuild is otherwise git-blind and will happily reinstall
    # stale code when local main was never pulled after a remote merge. Refuse
    # (unless --force) so the fix happens before the reinstall, not after.
    behind, err = _git_behind_origin(project_root)
    if err:
        print(f"  - Skipping git-drift check ({err})")
    elif behind and behind > 0:
        print(f"  [!!] Local checkout is {behind} commit(s) behind origin/main.")
        print(f"       {project_root}")
        print("       Rebuild would reinstall stale code. Run first:")
        print("         git pull --ff-only")
        if not force:
            print("       (or re-run with --force to rebuild anyway)")
            return 1
        print("       --force given: rebuilding from the behind checkout anyway.")
    else:
        print("  ✓ Checkout up to date with origin/main")
    print()

    # Step 1: Clear uv cache
    if UV_CACHE_DIR.exists():
        print(f"Clearing uv cache ({UV_CACHE_DIR})...")
        shutil.rmtree(UV_CACHE_DIR)
        print("  ✓ Cache cleared")
    else:
        print("  - No cache to clear")

    # Step 2: Uninstall
    print("Uninstalling agentwire-dev...")
    result = subprocess.run(
        ["uv", "tool", "uninstall", "agentwire-dev"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  ✓ Uninstalled")
    else:
        # Might not be installed, that's fine
        print("  - Not installed (continuing)")

    # Step 3: Reinstall from the source checkout resolved above.
    print(f"Installing from {project_root}...")
    result = subprocess.run(
        ["uv", "tool", "install", "."],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ✗ Install failed: {result.stderr}", file=sys.stderr)
        return 1

    print("  ✓ Installed")
    print()
    print("Rebuild complete. New version is active.")
    return 0


def cmd_uninstall(args) -> int:
    """Uninstall: clear uv cache and remove agentwire-dev tool."""
    print("Uninstalling agentwire-dev...")
    print()

    # Step 1: Clear uv cache
    if UV_CACHE_DIR.exists():
        print(f"Clearing uv cache ({UV_CACHE_DIR})...")
        shutil.rmtree(UV_CACHE_DIR)
        print("  ✓ Cache cleared")
    else:
        print("  - No cache to clear")

    # Step 2: Uninstall
    print("Uninstalling tool...")
    result = subprocess.run(
        ["uv", "tool", "uninstall", "agentwire-dev"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  ✓ Uninstalled")
    else:
        print(f"  - {result.stderr.strip() or 'Not installed'}")

    print()
    print("Uninstall complete.")
    print(f"To reinstall: cd {get_source_dir()} && uv tool install .")
    return 0


# === MCP Server Command ===


def cmd_mcp(args) -> int:
    """Run the MCP server on stdio.

    Exposes AgentWire capabilities as MCP tools for external agents
    like MoltBot, Claude Desktop, etc.
    """
    from .mcp_server import run_server
    run_server()
    return 0


# =============================================================================
# Handoff Commands (shareable conversation bundles — ai-handoff.md + show-the-story.html)
# =============================================================================


_HANDOFF_TEMPLATE = """\
<session_bundle version="1">
<title>{{ short title — what this session is about }}</title>

<metadata>
- cwd: __CWD__
- repo_url: __REPO_URL__
- branch: __BRANCH__
- commit: __COMMIT__
- session_type: claude-bypass
- model: __MODEL__
- started_at: {{ ISO timestamp }}
- ended_at: {{ ISO timestamp }}
- user_identity: {{ who }}
- mcp_servers: {{ comma-separated list }}
</metadata>

<environment>
{{ panes, channels, scheduler state, anything else the receiver can't see from cwd alone }}
</environment>

<instructions>
{{ The CLI prefilled this section with collected CLAUDE.md / rules / memory.
   Leave it alone unless you need to redact secrets. }}
__INSTRUCTIONS_BLOCK__
</instructions>

<project_state>
<git_status>
__GIT_STATUS__
</git_status>
<git_log>
__GIT_LOG__
</git_log>
<git_diff>
__GIT_DIFF__
</git_diff>
{{ optionally <file path="..."> blocks for key files }}
</project_state>

<conversation_summary>
<goal>{{ one sentence: what this session set out to do }}</goal>
<tldr>{{ one paragraph the receiver reads first }}</tldr>
<decisions>
<decision>
<title>{{ short name of decision }}</title>
<rationale>{{ why it was made }}</rationale>
<alternatives>
- {{ alternative 1 }}
- {{ alternative 2 }}
</alternatives>
</decision>
</decisions>
<dead_ends>
<dead_end>
<title>{{ thing tried }}</title>
<why>{{ why rejected — saves the receiver from retracing }}</why>
</dead_end>
</dead_ends>
<open_threads>
<thread>
<title>{{ unresolved item }}</title>
<note>{{ where to pick up }}</note>
</thread>
</open_threads>
<stats>
- turns: {{ N }}
- files_touched: {{ N }}
- tools_used: {{ N }}
- duration_minutes: {{ N }}
</stats>
</conversation_summary>

<journey>
<beat title="{{ short name }}">
<quote>{{ optional verbatim line from the conversation }}</quote>
<what_happened>{{ what changed at this beat }}</what_happened>
</beat>
</journey>

<recent_turns>
{{ Last ~10-20 turns, filtered: drop tool noise, keep user turns + decision-making
   assistant turns. Use markdown blockquotes or simple labelled blocks. }}
</recent_turns>

<handoff>
<one_sentence>{{ what the next agent should do first }}</one_sentence>
<resume_at>{{ specific file path / TODO / step number }}</resume_at>
<caveats>
- {{ permission boundary, e.g. "do not push" }}
- {{ env-specific note }}
</caveats>
</handoff>

<theme>
{
  "name": "{{ short slug, e.g. evening-debug }}",
  "mood": "{{ honest read of the session's emotional tone }}",
  "palette": {
    "bg": "#0e0f13",
    "surface": "#1a1d24",
    "fg": "#e2e8f0",
    "muted": "#64748b",
    "accent": "#5eead4",
    "accent_2": "#fbbf24",
    "border": "#2a2f3a"
  },
  "fonts": {
    "heading": "ui-monospace, 'JetBrains Mono', monospace",
    "body": "ui-sans-serif, system-ui, sans-serif"
  },
  "motion": "subtle"
}
</theme>
</session_bundle>
"""


def _handoff_artifacts_dir() -> Path:
    try:
        from .config import load_config
        cfg = load_config()
        return Path(str(cfg.artifacts.dir)).expanduser()
    except Exception:
        return Path.home() / ".agentwire" / "artifacts"


def _handoff_slug(title_hint: str | None = None) -> str:
    import re as _re
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if title_hint:
        clean = _re.sub(r"[^a-z0-9]+", "-", title_hint.lower()).strip("-")[:40]
        if clean:
            return f"handoff-{ts}-{clean}"
    return f"handoff-{ts}"


def _build_template(cwd: Path) -> str:
    """Pre-fill template with collected git + instructions chain."""
    from .handoff import git_state, instructions

    snap = git_state.snapshot(cwd)
    chain = instructions.collect(cwd)

    instructions_block_parts = []
    for instr in chain:
        instructions_block_parts.append(
            f'<file path="{instr.path}" kind="{instr.kind}">\n{instr.content}\n</file>'
        )
    instructions_block = "\n".join(instructions_block_parts) if instructions_block_parts else \
        "{{ no CLAUDE.md or memory files found — this bundle is portable but light on instructions }}"

    substitutions = {
        "__CWD__": str(cwd),
        "__REPO_URL__": snap.get("remote_url") or "{{ none }}",
        "__BRANCH__": snap.get("branch") or "{{ unknown }}",
        "__COMMIT__": snap.get("commit") or "{{ unknown }}",
        "__MODEL__": "claude-opus-4-7",
        "__INSTRUCTIONS_BLOCK__": instructions_block,
        "__GIT_STATUS__": snap.get("status") or "(clean)",
        "__GIT_LOG__": snap.get("log") or "(no commits)",
        "__GIT_DIFF__": snap.get("diff") or "(no uncommitted diff)",
    }
    text = _HANDOFF_TEMPLATE
    for key, value in substitutions.items():
        text = text.replace(key, value)
    return text


def cmd_handoff_init(args) -> int:
    """Create a handoff bundle directory and emit a pre-filled ai-handoff.md template.

    The agent then edits ai-handoff.md to add the session-specific summary,
    decisions, journey, and theme — the parts only the agent has context for.
    """
    json_mode = getattr(args, 'json', False)
    title_hint = getattr(args, 'title', None)
    output_dir = getattr(args, 'output_dir', None)

    base = Path(output_dir).expanduser() if output_dir else _handoff_artifacts_dir()
    bundle_dir = base / _handoff_slug(title_hint)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    cwd = Path.cwd()
    template_text = _build_template(cwd)
    handoff_path = bundle_dir / "ai-handoff.md"
    handoff_path.write_text(template_text, encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "cwd": str(cwd),
        "title_hint": title_hint or "",
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    if json_mode:
        _output_json({
            "success": True,
            "bundle_dir": str(bundle_dir),
            "ai_handoff_path": str(handoff_path),
            "manifest_path": str(bundle_dir / "manifest.json"),
            "instructions": (
                "Edit ai-handoff.md to fill in the {{ ... }} placeholders. "
                "Then run: agentwire handoff render <bundle_dir> --story"
            ),
        })
    else:
        print(f"Bundle dir: {bundle_dir}")
        print(f"Edit:       {handoff_path}")
        print()
        print("Next: fill in the placeholders, then run:")
        print(f"  agentwire handoff render {bundle_dir} --story")
    return 0


def cmd_handoff_render(args) -> int:
    """Render show-the-story.html from an existing ai-handoff.md."""
    from .handoff.parser import HandoffParseError, parse_file
    from .handoff.renderer import render_to_file

    json_mode = getattr(args, 'json', False)
    target = Path(args.path).expanduser()

    if target.is_dir():
        md_path = target / "ai-handoff.md"
        bundle_dir = target
    else:
        md_path = target
        bundle_dir = target.parent

    if not md_path.exists():
        msg = f"ai-handoff.md not found at {md_path}"
        if json_mode:
            _output_json({"success": False, "error": msg})
        else:
            print(msg, file=sys.stderr)
        return 1

    try:
        bundle = parse_file(md_path)
    except HandoffParseError as e:
        msg = f"Invalid ai-handoff.md: {e}"
        if json_mode:
            _output_json({"success": False, "error": msg, "path": str(md_path)})
        else:
            print(msg, file=sys.stderr)
        return 1

    outputs: dict[str, str] = {"ai_handoff_path": str(md_path)}

    if getattr(args, 'story', True):
        story_path = bundle_dir / "show-the-story.html"
        render_to_file(bundle, story_path)
        outputs["show_the_story_path"] = str(story_path)

    if json_mode:
        _output_json({"success": True, "bundle_dir": str(bundle_dir), **outputs})
    else:
        print(f"Bundle dir: {bundle_dir}")
        for k, v in outputs.items():
            print(f"  {k}: {v}")
    return 0


def cmd_handoff_list(args) -> int:
    """List past handoff bundles in the artifacts directory."""
    json_mode = getattr(args, 'json', False)
    base = _handoff_artifacts_dir()

    bundles = []
    if base.exists():
        for d in sorted(base.glob("handoff-*")):
            if not d.is_dir():
                continue
            handoff_md = d / "ai-handoff.md"
            story_html = d / "show-the-story.html"
            manifest_path = d / "manifest.json"
            manifest_data = {}
            if manifest_path.exists():
                try:
                    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            bundles.append({
                "name": d.name,
                "path": str(d),
                "ai_handoff_exists": handoff_md.exists(),
                "show_the_story_exists": story_html.exists(),
                "created_at": manifest_data.get("created_at", ""),
                "title_hint": manifest_data.get("title_hint", ""),
                "cwd": manifest_data.get("cwd", ""),
            })

    if json_mode:
        _output_json({"success": True, "bundles": bundles, "count": len(bundles)})
    else:
        if not bundles:
            print(f"No handoff bundles in {base}")
            return 0
        print(f"Handoff bundles in {base}:")
        for b in bundles:
            badge = []
            if b["ai_handoff_exists"]:
                badge.append("md")
            if b["show_the_story_exists"]:
                badge.append("html")
            print(f"  {b['name']:<60} [{','.join(badge) or '-'}] {b['title_hint']}")
    return 0


# === Hooks Commands ===

CLAUDE_HOOKS_DIR = Path.home() / ".claude" / "hooks"


# =============================================================================
# Roles Commands
# =============================================================================


def cmd_roles_list(args) -> int:
    """List available roles from all sources."""
    from .roles import parse_role_file

    json_mode = getattr(args, 'json', False)

    # Collect roles from all sources
    roles_data = []

    # User roles (~/.agentwire/roles/)
    user_roles_dir = Path.home() / ".agentwire" / "roles"
    if user_roles_dir.exists():
        for role_file in user_roles_dir.glob("*.md"):
            role = parse_role_file(role_file)
            if role:
                roles_data.append({
                    "name": role.name,
                    "description": role.description,
                    "source": "user",
                    "path": str(role_file),
                    "disallowed_tools": role.disallowed_tools,
                })

    # Bundled roles (agentwire/roles/)
    try:
        bundled_dir = Path(__file__).parent / "roles"
        if bundled_dir.exists():
            for role_file in bundled_dir.glob("*.md"):
                # Skip if user already has this role
                if any(r["name"] == role_file.stem for r in roles_data):
                    continue
                role = parse_role_file(role_file)
                if role:
                    roles_data.append({
                        "name": role.name,
                        "description": role.description,
                        "source": "bundled",
                        "path": str(role_file),
                        "disallowed_tools": role.disallowed_tools,
                        })
    except Exception:
        pass

    if json_mode:
        _output_json({"roles": roles_data})
        return 0

    if not roles_data:
        print("No roles found.")
        print("Create roles in: ~/.agentwire/roles/")
        return 0

    # Print table
    print("Available Roles:")
    print()
    print(f"{'Name':<20} {'Source':<10} {'Description':<40}")
    print("-" * 70)
    for r in sorted(roles_data, key=lambda x: x["name"]):
        desc = r["description"][:37] + "..." if len(r["description"]) > 40 else r["description"]
        print(f"{r['name']:<20} {r['source']:<10} {desc:<40}")

    print()
    print("User roles: ~/.agentwire/roles/")
    print("Use 'agentwire roles show <name>' for details")
    return 0


def cmd_projects_list(args) -> int:
    """List discovered projects."""
    from .projects import get_projects

    json_mode = getattr(args, 'json', False)
    machine_filter = getattr(args, 'machine', None)

    projects = get_projects(machine=machine_filter)

    if json_mode:
        _output_json({"projects": projects})
        return 0

    if not projects:
        print("No projects found.")
        print("Projects need a .agentwire.yml file in their directory.")
        return 0

    # Print table
    print(f"Discovered Projects ({len(projects)}):\n")
    print(f"{'Name':<25} {'Type':<15} {'Path':<40}")
    print("-" * 80)
    for p in projects:
        # Truncate long paths
        path = p["path"]
        if len(path) > 40:
            path = "..." + path[-37:]
        machine_suffix = f" @{p['machine']}" if p['machine'] != 'local' else ""
        print(f"{p['name']:<25} {p['type']:<15} {path:<40}{machine_suffix}")

    print()
    return 0


_VALID_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def cmd_projects_create(args) -> int:
    """Create a new local project: make the directory, optionally git-init or clone, and write .agentwire.yml."""
    from .config import get_config
    from .project_config import ProjectConfig, SessionType, ensure_gitignored, save_project_config

    name = (args.name or "").strip()
    clone_url = (getattr(args, "from_url", None) or "").strip() or None
    do_git_init = bool(getattr(args, "git_init", False))
    json_mode = getattr(args, "json", False)

    def _fail(msg: str) -> int:
        if json_mode:
            _output_json({"success": False, "error": msg})
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    if not name:
        return _fail("Project name is required")
    if not _VALID_PROJECT_NAME.match(name) or "/" in name or ".." in name:
        return _fail("Invalid project name (allowed: letters, digits, '.', '_', '-')")

    projects_dir = get_config().projects.dir.expanduser().resolve()
    projects_dir.mkdir(parents=True, exist_ok=True)
    project_path = projects_dir / name

    if project_path.exists():
        return _fail(f"Project already exists at {project_path}")

    try:
        if clone_url:
            result = subprocess.run(
                ["git", "clone", clone_url, str(project_path)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                return _fail(f"git clone failed: {result.stderr.strip() or 'unknown error'}")
        else:
            project_path.mkdir(parents=True, exist_ok=False)
            if do_git_init:
                result = subprocess.run(
                    ["git", "init"],
                    cwd=str(project_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    return _fail(f"git init failed: {result.stderr.strip() or 'unknown error'}")
    except subprocess.TimeoutExpired:
        return _fail("Operation timed out")
    except OSError as e:
        return _fail(f"Failed to create directory: {e}")

    gitignore_updated = ensure_gitignored(project_path)
    config = ProjectConfig(type=SessionType.from_str("claude-bypass"), roles=[], voice=None)
    if not save_project_config(config, project_path):
        return _fail("Created project directory but failed to write .agentwire.yml")

    payload = {
        "success": True,
        "name": name,
        "path": str(project_path),
        "machine": "local",
        "cloned": bool(clone_url),
        "git_init": do_git_init and not clone_url,
    }
    if json_mode:
        _output_json(payload)
    else:
        print(f"Created project '{name}' at {project_path}")
        if clone_url:
            print(f"  Cloned from: {clone_url}")
        elif do_git_init:
            print("  Initialized empty git repository")
        if gitignore_updated:
            print("  Added .agentwire.yml to .gitignore (personal config — keep it untracked)")
    return 0


def cmd_roles_show(args) -> int:
    """Show details for a specific role."""
    from .roles import discover_role, parse_role_file

    name = args.name
    json_mode = getattr(args, 'json', False)

    # Discover role
    role_path = discover_role(name)
    if not role_path:
        if json_mode:
            _output_json({"error": f"Role '{name}' not found"})
        else:
            print(f"Role '{name}' not found.", file=sys.stderr)
            print("Available locations:")
            print(f"  User: ~/.agentwire/roles/{name}.md")
            print(f"  Bundled: agentwire/roles/{name}.md")
        return 1

    role = parse_role_file(role_path)
    if not role:
        if json_mode:
            _output_json({"error": "Failed to parse role file"})
        else:
            print(f"Failed to parse role file: {role_path}", file=sys.stderr)
        return 1

    if json_mode:
        _output_json({
            "name": role.name,
            "description": role.description,
            "path": str(role_path),
            "tools": role.tools,
            "disallowed_tools": role.disallowed_tools,
            "color": role.color,
            "instructions": role.instructions,
        })
        return 0

    print(f"Role: {role.name}")
    print(f"Description: {role.description or '(none)'}")
    print(f"Path: {role_path}")
    if role.tools:
        print(f"Tools (whitelist): {', '.join(role.tools)}")
    if role.disallowed_tools:
        print(f"Disallowed Tools: {', '.join(role.disallowed_tools)}")
    print()
    if role.instructions:
        print("Instructions:")
        print("-" * 40)
        print(role.instructions)
        print("-" * 40)
    else:
        print("Instructions: (none)")

    return 0


def get_hooks_source() -> Path:
    """Get the path to the hooks directory in the installed package."""
    # First try: hooks directory inside the agentwire package
    package_dir = Path(__file__).parent
    hooks_dir = package_dir / "hooks"
    if hooks_dir.exists():
        return hooks_dir

    # Fallback: try importlib.resources (for installed packages)
    try:
        with importlib.resources.files("agentwire").joinpath("hooks") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find hooks directory in package")


CLAUDE_COMMANDS_DIR = Path.home() / ".claude" / "commands"


def get_commands_source() -> Path:
    """Get the path to the commands directory in the installed package."""
    package_dir = Path(__file__).parent
    commands_dir = package_dir / "commands"
    if commands_dir.exists():
        return commands_dir

    try:
        with importlib.resources.files("agentwire").joinpath("commands") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find commands directory in package")


def install_commands(force: bool = False) -> list[str]:
    """Symlink bundled slash commands into ~/.claude/commands/.

    Returns list of command names that were installed or updated.
    """
    try:
        commands_source = get_commands_source()
    except FileNotFoundError:
        return []

    CLAUDE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)

    installed = []
    for src_file in commands_source.glob("*.md"):
        target = CLAUDE_COMMANDS_DIR / src_file.name
        if target.exists() or target.is_symlink():
            if target.is_symlink() and target.resolve() == src_file.resolve() and not force:
                continue  # Already correctly symlinked
            target.unlink()

        target.symlink_to(src_file.resolve())
        installed.append(src_file.stem)

    return installed


CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


def get_skills_source() -> Path:
    """Get the path to the skills directory in the installed package.

    Mirrors get_commands_source(): resolves to the bundled package dir so the
    installed symlink is auto-current after `agentwire rebuild` (which reinstalls
    the wheel), never a transient checkout path.
    """
    package_dir = Path(__file__).parent
    skills_dir = package_dir / "skills"
    if skills_dir.exists():
        return skills_dir

    try:
        with importlib.resources.files("agentwire").joinpath("skills") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find skills directory in package")


def _managed_global_skills() -> list[str]:
    """Agentwire-owned skills that belong GLOBALLY in ~/.claude/skills/.

    Only `wiki` is global — the wiki store lives at ~/.agentwire/wiki/ and is
    usable from any session. The agentwire-* skills stay project-scoped (shipped
    via the repo's .claude/skills/, discovered per-project) and are NOT installed
    here. Third-party skills (cua-driver, shadcn-ui, …) are never touched.
    """
    return ["wiki"]


def _managed_skill_state(target: Path, source: Path) -> str:
    """Drift state of a managed global skill DIRECTORY: missing | stale | ok.

    Skills are directories, so unlike _managed_file_state this never compares
    bytes. A symlink is ok only when it resolves to the packaged source; a real
    directory (the hand-placed pre-#475 state) or a symlink pointing elsewhere is
    stale and must be removed before re-symlinking.
    """
    if target.is_symlink():
        if not target.exists():
            return "stale"  # dangling symlink
        return "ok" if target.resolve() == source.resolve() else "stale"
    if not target.exists():
        return "missing"
    return "stale"  # real dir/file occupying the slot


def _remove_skill_target(target: Path) -> None:
    """Clear whatever occupies a skill slot — symlink, real dir, or stray file."""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)


def install_skills(force: bool = False, copy: bool = False) -> dict[str, str]:
    """Install/refresh agentwire-owned global skills into ~/.claude/skills/.

    Each managed skill is a directory installed as a symlink (or copied with
    --copy) pointing at the packaged source, drift-aware: a correct symlink is
    left alone, a real-dir / wrong-symlink target is replaced.

    Returns {name: "installed" | "updated" | "current" | "missing-source"}.
    """
    try:
        skills_source = get_skills_source()
    except FileNotFoundError:
        print("  Warning: skills directory not found, skipping skill installation")
        return {}

    results: dict[str, str] = {}
    for name in _managed_global_skills():
        source = skills_source / name
        if not source.exists():
            print(f"  Warning: skill '{name}' not found in package, skipping")
            results[name] = "missing-source"
            continue

        target = CLAUDE_SKILLS_DIR / name
        state = _managed_skill_state(target, source)
        if state == "ok" and not force:
            results[name] = "current"
            continue

        existed = target.exists() or target.is_symlink()
        CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        _remove_skill_target(target)
        if copy:
            shutil.copytree(source, target)
        else:
            target.symlink_to(source.resolve(), target_is_directory=True)
        results[name] = "updated" if existed else "installed"

    return results


def skill_drift() -> dict[str, str]:
    """Drift state of agentwire-owned global skills.

    Returns {name: ok|stale|missing|source-unavailable}. Mirrors
    cli_safety.*_drift() so `agentwire doctor` can flag a hand-placed or drifted
    skill the same way it flags hook drift.

    `source-unavailable` means the packaged skill can't be resolved in the
    running context — e.g. doctor invoked from a SOURCE checkout, where skills
    only exist inside the built wheel (`agentwire/skills/`), not on disk. That is
    NOT a drift problem: there is nothing to install from, so doctor skips it
    rather than crying "missing". `missing`/`stale` are reserved for the case
    where the source IS resolvable (installed tool) and the installed copy is
    genuinely absent or wrong.
    """
    try:
        skills_source = get_skills_source()
    except FileNotFoundError:
        return {name: "source-unavailable" for name in _managed_global_skills()}

    drift: dict[str, str] = {}
    for name in _managed_global_skills():
        source = skills_source / name
        target = CLAUDE_SKILLS_DIR / name
        if not source.exists():
            drift[name] = "source-unavailable"
            continue
        drift[name] = _managed_skill_state(target, source)
    return drift


def register_hook_in_settings(event: str, hook_name: str) -> bool:
    """Register a hook under `event` in Claude's settings.json.

    Returns True if settings were updated, False if already configured.

    Claude Code hook format:
    {
      "hooks": {
        "<event>": [
          {
            "matcher": ".*",
            "hooks": [
              {"type": "command", "command": "~/.claude/hooks/<hook_name>"}
            ]
          }
        ]
      }
    }
    """
    settings_file = Path.home() / ".claude" / "settings.json"
    # Use ~ for portability
    hook_command = f"~/.claude/hooks/{hook_name}"

    # Load existing settings or create new
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    # Ensure hooks structure exists
    if "hooks" not in settings:
        settings["hooks"] = {}
    if event not in settings["hooks"]:
        settings["hooks"][event] = []

    # Check if already registered (check nested hooks array)
    for entry in settings["hooks"][event]:
        if "hooks" in entry:
            for h in entry["hooks"]:
                if h.get("command") == hook_command:
                    return False  # Already registered

    # Add hook with correct Claude Code format
    hook_entry = {
        "matcher": ".*",
        "hooks": [
            {"type": "command", "command": hook_command}
        ]
    }
    settings["hooks"][event].append(hook_entry)

    # Write back
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(settings, indent=2))

    return True


# Agentwire-owned files deployed by `hooks install`. Each entry:
# (filename in agentwire/hooks/, target directory, settings.json event or None)
def _managed_hook_files() -> list[tuple[str, Path, str | None]]:
    return [
        ("agentwire-permission.sh", CLAUDE_HOOKS_DIR, "PermissionRequest"),
        ("idle-handler.sh", CLAUDE_HOOKS_DIR, "Notification"),
        ("queue-processor.sh", Path.home() / ".agentwire", None),
    ]


def _managed_file_state(target: Path, source: Path) -> str:
    """Drift state of an agentwire-managed installed file: missing | stale | ok.

    Symlinks are ok when they resolve to the packaged source; regular files
    are ok when their content matches it byte-for-byte.
    """
    if target.is_symlink():
        if not target.exists():
            return "stale"  # dangling symlink
        return "ok" if target.resolve() == source.resolve() else "stale"
    if not target.exists():
        return "missing"
    try:
        return "ok" if target.read_bytes() == source.read_bytes() else "stale"
    except OSError:
        return "stale"


def _install_managed_file(source: Path, target: Path, force: bool = False, copy: bool = False) -> bool:
    """Install or refresh an agentwire-owned file (symlink by default).

    These files carry no user edits to preserve — any drift from the packaged
    source is replaced. Returns True if the target was created or updated.
    """
    if not force and _managed_file_state(target, source) == "ok":
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    if copy:
        shutil.copy2(source, target)
    else:
        target.symlink_to(source.resolve())
    target.chmod(0o755)
    return True


def install_hooks(force: bool = False, copy: bool = False) -> dict[str, str]:
    """Install/refresh all agentwire-owned hook files + settings.json registration.

    Returns {filename: "installed" | "updated" | "current" | "missing-source"}.
    """
    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        print("  Warning: hooks directory not found, skipping hook installation")
        return {}

    results: dict[str, str] = {}
    for hook_name, target_dir, event in _managed_hook_files():
        source = hooks_source / hook_name
        if not source.exists():
            print(f"  Warning: {hook_name} not found in package, skipping")
            results[hook_name] = "missing-source"
            continue

        target = target_dir / hook_name
        existed = target.exists() or target.is_symlink()
        if _install_managed_file(source, target, force=force, copy=copy):
            results[hook_name] = "updated" if existed else "installed"
        else:
            results[hook_name] = "current"

        if event:
            register_hook_in_settings(event, hook_name)

    # Heal the full damage-control surface (hook scripts + rules + tooldefs +
    # PreToolUse matchers), not just the settings.json matchers. This closes the
    # documented post-rebuild gap: CLAUDE.md tells users to re-run `hooks install`
    # after a rebuild, so it must actually sync the DC files/rules — drift-aware,
    # never clobbering a customized rule.
    try:
        from agentwire.cli_safety import heal_damage_control
        heal_damage_control(quiet=True)
    except Exception:
        pass

    # Global skills (currently just /wiki) are agentwire-owned too, and rotted
    # silently because nothing ever resynced the hand-placed copies. Heal them
    # on the same install pass — drift-aware, like the hooks above.
    results.update(install_skills(force=force, copy=copy))

    return results


def cmd_hooks_install(args) -> int:
    """Install agentwire-owned hook files and slash commands for AgentWire integration."""
    results = install_hooks(force=args.force, copy=args.copy)
    for hook_name, target_dir, _event in _managed_hook_files():
        state = results.get(hook_name)
        if state in ("installed", "updated"):
            print(f"{state.capitalize()} {hook_name} -> {target_dir / hook_name}")
        elif state == "current":
            print(f"{hook_name} already current.")

    installed_commands = install_commands(force=args.force)
    if installed_commands:
        print(f"\nInstalled slash commands to {CLAUDE_COMMANDS_DIR}:")
        for name in installed_commands:
            print(f"  /{name}")
    else:
        print("Slash commands already installed.")

    refreshed_skills = False
    for name in _managed_global_skills():
        state = results.get(name)
        if state in ("installed", "updated"):
            print(f"{state.capitalize()} skill -> /{name} ({CLAUDE_SKILLS_DIR / name})")
            refreshed_skills = True
    if not refreshed_skills:
        print("Skills already current.")

    return 0


def unregister_hook_from_settings(event: str, hook_name: str) -> bool:
    """Remove a hook registered under `event` from Claude's settings.json.

    Returns True if settings were updated, False if not found.
    """
    settings_file = Path.home() / ".claude" / "settings.json"
    hook_command = f"~/.claude/hooks/{hook_name}"

    if not settings_file.exists():
        return False

    try:
        settings = json.loads(settings_file.read_text())
    except json.JSONDecodeError:
        return False

    if "hooks" not in settings or event not in settings["hooks"]:
        return False

    # Filter out entries containing our hook
    original_len = len(settings["hooks"][event])
    new_entries = []
    for entry in settings["hooks"][event]:
        if "hooks" in entry:
            # Check if any hook in this entry matches ours
            has_our_hook = any(h.get("command") == hook_command for h in entry["hooks"])
            if not has_our_hook:
                new_entries.append(entry)
        else:
            new_entries.append(entry)

    settings["hooks"][event] = new_entries

    if len(settings["hooks"][event]) == original_len:
        return False  # Hook wasn't registered

    # Clean up empty structures
    if not settings["hooks"][event]:
        del settings["hooks"][event]
    if not settings["hooks"]:
        del settings["hooks"]

    # Write back
    settings_file.write_text(json.dumps(settings, indent=2))
    return True


def is_hook_registered(event: str, hook_name: str) -> bool:
    """Check if a hook is registered under `event` in Claude's settings.json."""
    settings_file = Path.home() / ".claude" / "settings.json"
    hook_command = f"~/.claude/hooks/{hook_name}"

    if not settings_file.exists():
        return False

    try:
        settings = json.loads(settings_file.read_text())
    except json.JSONDecodeError:
        return False

    if "hooks" not in settings or event not in settings["hooks"]:
        return False

    # Check nested hooks array for our command
    for entry in settings["hooks"][event]:
        if "hooks" in entry:
            for h in entry["hooks"]:
                if h.get("command") == hook_command:
                    return True
    return False


def cmd_hooks_uninstall(args) -> int:
    """Remove all agentwire-owned hook files and their settings.json registration."""
    removed_any = False
    for hook_name, target_dir, event in _managed_hook_files():
        target = target_dir / hook_name
        if target.exists() or target.is_symlink():
            target.unlink()
            print(f"Removed: {target}")
            removed_any = True
        if event and unregister_hook_from_settings(event, hook_name):
            print(f"Unregistered {hook_name} from Claude settings.json")

    if not removed_any:
        print("No hooks installed")

    return 0


def cmd_hooks_status(args) -> int:
    """Check agentwire-owned hook files and tmux portal sync hooks."""
    print("=== AgentWire Hooks ===")
    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        hooks_source = None

    for hook_name, target_dir, event in _managed_hook_files():
        target = target_dir / hook_name
        print(f"{hook_name}:")

        if not (target.exists() or target.is_symlink()):
            print("  Status: not installed — run 'agentwire hooks install'")
            continue

        kind = "symlink" if target.is_symlink() else "copy"
        if hooks_source and (hooks_source / hook_name).exists():
            state = _managed_file_state(target, hooks_source / hook_name)
            drift = "" if state == "ok" else " — STALE, run 'agentwire hooks install'"
        else:
            drift = " — packaged source not found, drift unknown"
        print(f"  Status: installed ({kind}){drift}")
        location = f"{target} -> {target.resolve()}" if target.is_symlink() else str(target)
        print(f"  Location: {location}")

        if event:
            if is_hook_registered(event, hook_name):
                print(f"  Registered: yes ({event} in ~/.claude/settings.json)")
            else:
                print("  Registered: NO - run 'agentwire hooks install' to fix")

    # Tmux portal sync hooks
    print("\n=== Tmux Portal Sync Hooks ===")
    try:
        # Check global hooks first
        global_result = subprocess.run(
            ["tmux", "show-hooks", "-g"],
            capture_output=True,
            text=True,
        )
        global_hooks = global_result.stdout.strip()

        print("Global hooks:")
        has_global_created = "session-created" in global_hooks
        has_global_closed = "session-closed" in global_hooks

        if has_global_created or has_global_closed:
            parts = []
            if has_global_created:
                parts.append("session-created")
            if has_global_closed:
                parts.append("session-closed")
            print(f"  {', '.join(parts)}")
        else:
            print("  none (run 'agentwire portal restart' to install)")

        # Get list of sessions for per-session hooks
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("\nNo tmux sessions running")
            return 0

        sessions = result.stdout.strip().split("\n") if result.stdout.strip() else []

        if sessions:
            print("\nPer-session hooks:")
            for session in sessions:
                hooks_result = subprocess.run(
                    ["tmux", "show-hooks", "-t", session],
                    capture_output=True,
                    text=True,
                )
                hooks_output = hooks_result.stdout.strip()

                has_session_closed = "session-closed" in hooks_output
                has_kill_pane = "after-kill-pane" in hooks_output

                status_parts = []
                if has_session_closed:
                    status_parts.append("session-closed")
                if has_kill_pane:
                    status_parts.append("after-kill-pane")

                if status_parts:
                    print(f"  {session}: {', '.join(status_parts)}")
                else:
                    print(f"  {session}: none")

    except Exception as e:
        print(f"Error checking tmux hooks: {e}")

    return 0


# === Tunnel Commands ===


def cmd_tunnels_up(args) -> int:
    """Create all required tunnels."""
    from .network import NetworkContext
    from .tunnels import TunnelManager

    ctx = NetworkContext.from_config()
    manager = TunnelManager()
    required = ctx.get_required_tunnels()

    if not required:
        print("No tunnels required for this machine's configuration.")
        print("(All services run locally or no remote services configured)")
        return 0

    print("Creating tunnels for this machine...\n")

    all_success = True
    for i, spec in enumerate(required, 1):
        # Get service name for display
        service_name = _get_service_for_tunnel(ctx, spec)

        print(f"[{i}/{len(required)}] {service_name} (localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port})")

        status = manager.create_tunnel(spec, ctx)

        if status.status == "up":
            if status.error:
                # Tunnel up but service not responding
                print(f"      ! Tunnel created (PID {status.pid})")
                print(f"      ! Warning: {status.error}")
            else:
                print(f"      + Tunnel created (PID {status.pid})")
        else:
            all_success = False
            print(f"      x Failed: {status.error}")
            _print_tunnel_help(spec, status.error)

        print()

    if all_success:
        print("All tunnels up. Services should be reachable.")
    else:
        print("Some tunnels failed. Check errors above.")
        return 1

    return 0


def cmd_tunnels_down(args) -> int:
    """Tear down all tunnels."""
    from .tunnels import TunnelManager

    manager = TunnelManager()
    count = manager.destroy_all_tunnels()

    if count == 0:
        print("No active tunnels to tear down.")
    else:
        print(f"Killed {count} tunnel(s).")

    return 0


def cmd_tunnels_status(args) -> int:
    """Show tunnel health."""
    from .network import NetworkContext
    from .tunnels import TunnelManager

    ctx = NetworkContext.from_config()
    manager = TunnelManager()

    # Get both required and active tunnels
    required = ctx.get_required_tunnels()
    active = manager.list_tunnels()

    print("AgentWire Tunnels")
    print("-" * 55)

    if not required and not active:
        print("\nNo tunnels configured or active.")
        print("(All services run locally or no remote services configured)")
        return 0

    # Show required tunnels
    for spec in required:
        service_name = _get_service_for_tunnel(ctx, spec)

        print(f"\n{service_name} (localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port})")

        status = manager.check_tunnel(spec)

        if status.status == "up":
            print(f"  Status: + UP (PID {status.pid})")
        elif status.status == "down":
            print("  Status: - DOWN")
        else:
            print("  Status: x ERROR")
            if status.error:
                print(f"  Error: {status.error}")

    # Show any orphaned tunnels (active but not required)
    required_ids = {s.id for s in required}
    orphaned = [t for t in active if t.spec.id not in required_ids]
    if orphaned:
        print("\n" + "-" * 55)
        print("\nOrphaned tunnels (active but no longer required):")
        for t in orphaned:
            print(f"  localhost:{t.spec.local_port} -> {t.spec.remote_machine}:{t.spec.remote_port}")
            print(f"    PID: {t.pid}, Status: {t.status}")

    print("\n" + "-" * 55)

    # Show next steps
    down_tunnels = [s for s in required if manager.check_tunnel(s).status != "up"]
    if down_tunnels:
        print("To create missing tunnels: agentwire tunnels up")

    return 0


def cmd_tunnels_check(args) -> int:
    """Verify tunnels are working with health checks."""
    from .network import NetworkContext
    from .tunnels import TunnelManager, test_service_health

    ctx = NetworkContext.from_config()
    manager = TunnelManager()
    required = ctx.get_required_tunnels()

    if not required:
        print("No tunnels required for this machine.")
        return 0

    print("Checking tunnel health...\n")

    all_healthy = True
    for spec in required:
        service_name = _get_service_for_tunnel(ctx, spec)
        status = manager.check_tunnel(spec)

        if status.status == "up":
            # Also test the actual service through the tunnel
            url = f"http://localhost:{spec.local_port}/health"
            healthy, err = test_service_health(url, timeout=3)

            if healthy:
                print(f"+ {service_name}: healthy")
            else:
                print(f"! {service_name}: tunnel up but service not responding")
                if err:
                    print(f"  {err}")
                all_healthy = False
        elif status.status == "down":
            print(f"x {service_name}: down")
            all_healthy = False
        else:
            print(f"x {service_name}: error - {status.error}")
            all_healthy = False

    if all_healthy:
        print("\nAll tunnels healthy.")
        return 0
    else:
        print("\nSome tunnels need attention. Run: agentwire tunnels up")
        return 1


def _get_service_for_tunnel(ctx, spec) -> str:
    """Get human-readable service name for a tunnel spec."""
    # Check which service this tunnel is for
    for service_name in ["portal", "tts"]:
        service_config = getattr(ctx.config.services, service_name, None)
        if service_config and service_config.machine == spec.remote_machine and service_config.port == spec.remote_port:
            return f"Portal -> {service_name.upper()}" if service_name != "portal" else "Portal"

    return f"Tunnel to {spec.remote_machine}"


def _print_tunnel_help(spec, error: str) -> None:
    """Print helpful diagnostics for tunnel errors."""
    if not error:
        return

    error_lower = error.lower()

    print("\n      Possible causes:")

    if "port" in error_lower and "in use" in error_lower:
        print("        1. Another process is using this port")
        print("        2. A previous tunnel wasn't cleaned up")
        print("\n      To diagnose:")
        print(f"        lsof -i :{spec.local_port}    # Find process using port")
        print("        agentwire tunnels down        # Clean up stale tunnels")

    elif "permission denied" in error_lower:
        print("        1. SSH key not authorized on remote machine")
        print("        2. Wrong user configured")
        print("\n      To fix:")
        print(f"        ssh-copy-id {spec.remote_machine}")

    elif "host key" in error_lower:
        print("        1. Remote machine was reinstalled/changed")
        print("        2. Possible security issue (man-in-the-middle)")
        print("\n      If expected, fix with:")
        print(f"        ssh-keygen -R {spec.remote_machine}")

    elif "connection refused" in error_lower:
        print("        1. SSH server not running on remote")
        print("        2. Firewall blocking port 22")
        print("\n      To diagnose:")
        print(f"        ssh {spec.remote_machine} echo ok")

    elif "timed out" in error_lower or "no route" in error_lower:
        print("        1. Machine is powered off or unreachable")
        print("        2. Network connectivity issue")
        print("\n      To diagnose:")
        print(f"        ping {spec.remote_machine}")

    elif "not responding" in error_lower:
        print("        1. Remote service not started")
        print("        2. Remote service on wrong port")
        print("\n      To diagnose:")
        print(f"        ssh {spec.remote_machine} 'lsof -i :{spec.remote_port}'")


# =============================================================================
# Task Commands (Scheduled Workloads)
# =============================================================================

# Exit codes for ensure command (documented in CLAUDE.md)
ENSURE_EXIT_COMPLETE = 0
ENSURE_EXIT_FAILED = 1
ENSURE_EXIT_INCOMPLETE = 2
ENSURE_EXIT_LOCK_CONFLICT = 3
ENSURE_EXIT_PRE_FAILURE = 4
ENSURE_EXIT_TIMEOUT = 5
ENSURE_EXIT_SESSION_ERROR = 6
ENSURE_EXIT_USAGE_LIMIT = 7


def _ensure_remote(args, session: str, machine_id: str, json_mode: bool) -> int:
    """Delegate `ensure` to the remote machine via SSH.

    When the session target is `name@machine`, we reconstruct the full
    `agentwire ensure` command and run it on the remote machine natively.
    All local concerns (locking, idle detection, pre/post commands, summary
    files) happen on the remote machine where the session actually lives.
    """
    import shlex

    machine = _get_machine_config(machine_id)
    if machine is None:
        return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json", exit_code=ENSURE_EXIT_SESSION_ERROR)

    host = machine.get("host", machine_id)
    user = machine.get("user")
    port = machine.get("port")
    ssh_target = f"{user}@{host}" if user else host

    # Translate local project path to remote equivalent
    remote_project = None
    if hasattr(args, 'project') and args.project:
        local_path = Path(args.project).expanduser().resolve()
        # Get local projects dir from config
        config = load_config()
        local_projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser().resolve()
        # Get remote projects dir from machine config (or default)
        remote_projects_dir = machine.get("projects_dir", "~/projects")
        try:
            relative = local_path.relative_to(local_projects_dir)
            remote_project = f"{remote_projects_dir}/{relative}"
        except ValueError:
            # Path not under local projects dir — use basename only
            remote_project = f"{remote_projects_dir}/{local_path.name}"

    # Reconstruct ensure command for the remote (session without @machine)
    cmd_parts = ["agentwire", "ensure", "-s", session, "--task", args.task, "--json"]
    if remote_project:
        cmd_parts.extend(["--project", remote_project])
    if getattr(args, 'wait_lock', False):
        cmd_parts.append("--wait-lock")
    if getattr(args, 'lock_timeout', 60) != 60:
        cmd_parts.extend(["--lock-timeout", str(args.lock_timeout)])
    if getattr(args, 'skip_if_locked', False):
        cmd_parts.append("--skip-if-locked")

    remote_cmd = f"bash -l -c {shlex.quote(' '.join(shlex.quote(p) for p in cmd_parts))}"

    ssh_cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if port:
        ssh_cmd.extend(["-p", str(port)])
    ssh_cmd.extend([ssh_target, remote_cmd])

    # Stream output in real-time — ensure can run for tens of minutes
    try:
        proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            print(line, end="", flush=True)
        proc.wait()
        return proc.returncode
    except Exception as e:
        return _output_result(False, json_mode, f"SSH to {machine_id} failed: {e}", exit_code=ENSURE_EXIT_SESSION_ERROR)


def cmd_ensure(args) -> int:
    """Run a named task with reliable session management.

    Full lifecycle:
    1. Acquire lock (fail if locked, or wait with --wait-lock)
    2. Ensure session exists and is healthy
    3. Wait for session to be idle
    4. Run pre-commands, validate outputs
    5. Send templated prompt
    6. Wait for idle, send system summary prompt
    7. Parse summary file for status
    8. Send on_task_end prompt if defined
    9. Run post-commands
    10. Handle retries on failure
    """
    from .completion import (
        get_summary_prompt,
    )
    from .locking import LockConflict, LockTimeout, session_lock
    from .tasks import (
        TaskNotFound,
        TaskValidationError,
        load_task,
        validate_task,
    )
    from .templating import TemplateContext, preview_template

    session_name = args.session
    task_name = args.task
    dry_run = getattr(args, 'dry_run', False)
    wait_lock = getattr(args, 'wait_lock', False)
    lock_timeout = getattr(args, 'lock_timeout', 60)
    skip_if_locked = getattr(args, 'skip_if_locked', False)
    json_mode = getattr(args, 'json', False)

    # Parse session target
    session, machine_id = _parse_session_target(session_name)

    if machine_id:
        return _ensure_remote(args, session, machine_id, json_mode)

    # A session parked on a usage limit is waiting out the reset — never
    # prompt or re-dispatch into it. The watchdog resumes it.
    from .usage_limit import is_parked
    if is_parked(session):
        return _output_result(
            False, json_mode,
            f"Session '{session}' is parked on a usage limit (auto-resumes after reset)",
            exit_code=ENSURE_EXIT_USAGE_LIMIT,
        )

    # Find project path from --project flag, or session's working directory
    if hasattr(args, 'project') and args.project:
        project_path = Path(args.project).expanduser().resolve()
    else:
        project_path = _get_session_project_path(session)

    if not project_path.exists():
        return _output_result(False, json_mode, f"Project path not found: {project_path}", exit_code=ENSURE_EXIT_SESSION_ERROR)

    # Load task configuration
    try:
        task = load_task(project_path, task_name)
    except TaskNotFound as e:
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_SESSION_ERROR)
    except TaskValidationError as e:
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_SESSION_ERROR)

    # Validate task
    issues = validate_task(task)
    if issues:
        return _output_result(False, json_mode, f"Task validation failed: {', '.join(issues)}", exit_code=ENSURE_EXIT_SESSION_ERROR)

    # Determine shell
    shell = task.shell or "/bin/sh"

    # Initialize template context
    ctx = TemplateContext(
        session=session,
        task=task_name,
        project_root=str(project_path),
    )

    # Dry run mode
    if dry_run:
        print("=== DRY RUN ===\n")
        print(f"Session: {session}")
        print(f"Task: {task_name}")
        print(f"Shell: {shell}")
        print(f"Idle timeout: {task.idle_timeout}s")
        print(f"Retries: {task.retries}")
        print()

        if task.pre:
            print("Pre-commands (would execute):")
            for pre in task.pre:
                req = " (required)" if pre.required else ""
                val = f" validate: {pre.validate}" if pre.validate else ""
                print(f"  {pre.name}: {pre.cmd}{req}{val}")
            print()

        print("Prompt (with placeholders for pre-outputs):")
        print(preview_template(task.prompt, ctx))
        print()

        print("System summary prompt:")
        print(get_summary_prompt("<generated-filename>"))
        print()

        if task.on_task_end:
            print("On task end prompt:")
            print(preview_template(task.on_task_end, ctx))
            print()

        if task.post:
            print("Post-commands (would execute):")
            for cmd in task.post:
                print(f"  {preview_template(cmd, ctx)}")
            print()

        if task.output.save:
            print(f"Save output to: {preview_template(task.output.save, ctx)}")

        return 0

    # Acquire lock
    try:
        with session_lock(session, wait=wait_lock, timeout=lock_timeout):
            return _run_ensure_task(
                args, session, task, ctx, shell, project_path, json_mode
            )
    except LockConflict as e:
        if skip_if_locked:
            return 0
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_LOCK_CONFLICT)
    except LockTimeout as e:
        if skip_if_locked:
            return 0
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_LOCK_CONFLICT)


def _setup_task_branch(project_path, task, json_mode) -> tuple[str, str | None]:
    """Set up git branch for a task with starting_ref.

    Checks out starting_ref, pulls latest, creates the work branch.

    Returns:
        (work_branch_name, error_message) — error_message is None on success.
    """
    from .tasks import PreCommandError  # noqa: F401 (used for caller)

    starting_ref = task.starting_ref

    # Verify the ref exists
    result = subprocess.run(
        ["git", "rev-parse", "--verify", starting_ref],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "", f"starting_ref '{starting_ref}' not found in {project_path}"

    # Checkout the starting ref
    checkout = subprocess.run(
        ["git", "checkout", starting_ref],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        return "", f"Failed to checkout '{starting_ref}': {checkout.stderr.strip()}"

    # Pull if it's a branch (not detached HEAD)
    head_check = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        cwd=project_path,
        capture_output=True,
    )
    if head_check.returncode == 0:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=project_path,
            capture_output=True,
        )

    # Determine work branch name
    work_branch = task.work_branch
    if not work_branch:
        today = datetime.date.today().isoformat()
        work_branch = f"agent/{task.name}-{today}"

    # Handle collision: append -2, -3, ... until name is free
    check = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{work_branch}"],
        cwd=project_path,
        capture_output=True,
    )
    if check.returncode == 0:
        n = 2
        base = work_branch
        while True:
            candidate = f"{base}-{n}"
            check = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{candidate}"],
                cwd=project_path,
                capture_output=True,
            )
            if check.returncode != 0:
                work_branch = candidate
                break
            n += 1

    # Create and checkout work branch
    create = subprocess.run(
        ["git", "checkout", "-b", work_branch],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        return "", f"Failed to create work branch '{work_branch}': {create.stderr.strip()}"

    if not json_mode:
        print(f"Branch: {work_branch} (from {starting_ref})")

    return work_branch, None


def _create_task_pr(project_path, task, work_branch, last_summary, json_mode) -> str | None:
    """Commit, push, and open a PR for completed task work.

    Returns:
        PR URL if created, None if skipped or failed.
    """
    pr_target = task.pr_target or task.starting_ref

    # Check for uncommitted changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    has_changes = bool(status.stdout.strip())

    if not has_changes:
        if not json_mode:
            print("No changes to commit — skipping PR creation")
        # Still reset to starting_ref
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Commit all changes
    today = datetime.date.today().isoformat()
    commit_msg = f"chore: agent task {task.name} ({today})"
    subprocess.run(["git", "add", "-A"], cwd=project_path, capture_output=True)
    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        if not json_mode:
            print(f"Warning: commit failed: {commit_result.stderr.strip()}")
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Push branch
    push = subprocess.run(
        ["git", "push", "-u", "origin", work_branch],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        if not json_mode:
            print(f"Warning: push failed: {push.stderr.strip()}")
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Check gh is available
    gh_check = subprocess.run(["which", "gh"], capture_output=True)
    if gh_check.returncode != 0:
        if not json_mode:
            print("Warning: 'gh' not found — skipping PR creation (branch pushed)")
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Create PR
    pr_title = f"agent: {task.name} ({today})"
    pr_body = last_summary if last_summary else f"Automated changes from agent task `{task.name}`."
    pr_cmd = [
        "gh", "pr", "create",
        "--base", pr_target,
        "--head", work_branch,
        "--title", pr_title,
        "--body", pr_body,
    ]
    if task.pr_draft:
        pr_cmd.append("--draft")

    pr_result = subprocess.run(
        pr_cmd,
        cwd=project_path,
        capture_output=True,
        text=True,
    )

    pr_url = None
    if pr_result.returncode == 0:
        pr_url = pr_result.stdout.strip()
        if not json_mode:
            print(f"PR created: {pr_url}")
    else:
        if not json_mode:
            print(f"Warning: PR creation failed: {pr_result.stderr.strip()}")

    # Reset to starting_ref
    subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)

    return pr_url


def _run_ensure_task(args, session, task, ctx, shell, project_path, json_mode) -> int:
    """Run the task (called within lock context).

    Uses hook-based completion detection:
    1. Write task context file (tells hook a scheduled task is running)
    2. Send task prompt
    3. Hook handles: first idle → send summary prompt
    4. Poll for summary file (agent writes it after receiving summary prompt)
    5. Parse summary and return result
    """
    from .completion import (
        CompletionTimeout,
        clear_task_context,
        generate_summary_filename,
        status_to_exit_code,
        wait_for_completion_signal,
        write_task_context,
    )
    from .tasks import PreCommandError, run_post_command, run_pre_command
    from .templating import TemplateError, expand_all

    max_attempts = task.retries + 1
    last_status = "incomplete"
    last_summary = ""

    for attempt in range(1, max_attempts + 1):
        ctx.attempt = attempt

        if not json_mode and max_attempts > 1:
            print(f"Attempt {attempt}/{max_attempts}")

        # Ensure session exists and has agent running.
        # The scheduler may have pre-created this session with --model and --type
        # overrides via _pre_create_session(). Don't kill it — just wait for agent.
        if not tmux_session_exists(session):
            if not json_mode:
                print(f"Creating session '{session}'...")

            # Fork starting_session if configured (carries over Claude conversation context)
            if task.starting_session and task.starting_session != session:
                if tmux_session_exists(task.starting_session):
                    if not json_mode:
                        print(f"Forking context from session '{task.starting_session}'...")
                    fork_result = subprocess.run(
                        ["agentwire", "fork", "-s", task.starting_session, "-t", session, "--json"],
                        capture_output=True, text=True,
                    )
                    if fork_result.returncode != 0 and not json_mode:
                        print("Warning: context fork failed, starting fresh session")
                elif not json_mode:
                    print(f"Warning: starting_session '{task.starting_session}' not found, starting fresh")

            if not tmux_session_exists(session):
                class NewArgs:
                    def __init__(self, task_role):
                        self.session = session
                        self.path = str(project_path)
                        self.force = False
                        self.type = None
                        self.roles = task_role if task_role else None
                        self.model = None
                        self.json = json_mode

                from . import session_cli
                result = session_cli.cmd_new(NewArgs(task.role))
                if result != 0:
                    return _output_result(False, json_mode, f"Failed to create session '{session}'", exit_code=ENSURE_EXIT_SESSION_ERROR)

        # Wait for agent to be ready to accept input.
        # Handles both freshly-created sessions (agent still loading) and
        # pre-created sessions from scheduler (agent may be mid-startup).
        if not json_mode:
            print("Waiting for agent to be ready...")
        from agentwire.session_ready import wait_for_session_ready
        if not wait_for_session_ready(session, timeout=30):
            # Agent never started — session is dead, bail out
            if not json_mode:
                print(f"Agent not ready in session '{session}' after 30s")
            return _output_result(False, json_mode, f"Agent not running in session '{session}'", exit_code=ENSURE_EXIT_SESSION_ERROR)

        # Set up work branch if starting_ref is configured
        work_branch = None
        if task.starting_ref:
            work_branch, branch_error = _setup_task_branch(project_path, task, json_mode)
            if branch_error:
                return _output_result(False, json_mode, branch_error, exit_code=ENSURE_EXIT_PRE_FAILURE)

        # Run pre-commands
        if task.pre:
            if not json_mode:
                print("Running pre-commands...")

            for pre in task.pre:
                try:
                    output = run_pre_command(pre, shell, project_path)
                    ctx.set_pre_output(pre.name, output)
                    if not json_mode:
                        print(f"  {pre.name}: {len(output)} chars")
                except PreCommandError as e:
                    if work_branch and task.starting_ref:
                        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
                    return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_PRE_FAILURE)

        # Expand prompt
        try:
            prompt = expand_all(task.prompt, ctx)
        except TemplateError as e:
            return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_PRE_FAILURE)

        # Generate summary filename (scoped to session to avoid collisions)
        summary_filename = generate_summary_filename(session, task.name)
        summary_path = project_path / summary_filename
        ctx.summary_file = summary_filename

        # Ensure .agentwire directory exists
        (project_path / ".agentwire").mkdir(exist_ok=True)

        # Create iterations directory for loop tasks
        if task.mode == "loop":
            (project_path / ".agentwire" / "iterations").mkdir(exist_ok=True)

        # Clear any stale completion signal from a previous run
        # This prevents immediate return if a previous run's signal wasn't cleaned up
        clear_task_context(session)

        # Write task context for hook coordination
        # Hook will: first idle → send summary prompt (ensure polls for summary file directly)
        # Loop mode: hook iterates (review → re-prompt) until complete or max_iterations
        write_task_context(
            session=session,
            task_name=task.name,
            summary_file=summary_filename,
            attempt=attempt,
            exit_on_complete=task.exit_on_complete,
            mode=task.mode,
            max_iterations=task.max_iterations,
            iteration=1,
            loop_review=task.loop_review,
            loop_delay=task.loop_delay,
            original_prompt=prompt,
        )

        # Find previous summaries for this task to give the agent context
        summary_glob = f".agentwire/task-summary-{session}-{task.name}-*.md"
        prev_summaries = sorted(
            project_path.glob(summary_glob),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
        if prev_summaries:
            prompt += "\n\nPrevious task summaries (consider them when generating your output):"
            for p in prev_summaries:
                prompt += f"\n- {p}"

        if not json_mode:
            print("Sending task prompt...")

        # Send task prompt using pane_manager for proper multi-line handling
        pane_manager.send_to_pane(session, 0, prompt, enter=True)

        # Wait for completion signal from hook
        if not json_mode:
            print("Waiting for task completion...")

        try:
            signal = wait_for_completion_signal(
                session, summary_path=summary_path
            )
            last_status = signal.get("status", "incomplete")
            last_summary = signal.get("summary", "")
            ctx.status = last_status
            ctx.summary = last_summary
        except CompletionTimeout:
            # Don't clear task context here — the hook may still need it.
            # Hook cleans up after itself (exit_on_complete kills session).
            # Task context files are cleared at the START of next run.
            last_status = "incomplete"
            last_summary = "Timeout waiting for task completion"
            if attempt < max_attempts:
                if not json_mode:
                    print(f"Timeout, retrying in {task.retry_delay}s...")
                time.sleep(task.retry_delay)
                continue
            break

        # Don't clear task context here — hook owns context file lifecycle.
        # ensure waits for hook to delete it (signals cleanup complete).

        if last_status == "usage_limit":
            # Session parked mid-task — skip on_task_end/post/PR; the watchdog
            # nudges it after reset and the idle hook finishes the task.
            if not json_mode:
                print("Usage limit hit — session parked, auto-resumes after reset")
            break

        if not json_mode:
            print(f"Task status: {last_status}")
            if last_summary:
                print(f"Summary: {last_summary}")

        # on_task_end: send additional prompt after summary is written
        # Note: we don't wait for this to complete - it's fire-and-forget
        if task.on_task_end:
            try:
                end_prompt = expand_all(task.on_task_end, ctx)
                pane_manager.send_to_pane(session, 0, end_prompt, enter=True)
                if not json_mode:
                    print("Sent on_task_end prompt (not waiting for completion)")
            except TemplateError as e:
                if not json_mode:
                    print(f"Warning: template error in on_task_end: {e}")

        # Capture output
        output_result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{task.output.capture}"],
            capture_output=True,
            text=True,
        )
        ctx.output = output_result.stdout if output_result.returncode == 0 else ""

        # Run post-commands
        if task.post:
            if not json_mode:
                print("Running post-commands...")

            for cmd in task.post:
                try:
                    expanded_cmd = expand_all(cmd, ctx)
                    rc, stdout, stderr = run_post_command(expanded_cmd, shell, project_path)
                    if rc != 0 and not json_mode:
                        print(f"  Warning: post-command failed: {stderr}")
                except TemplateError as e:
                    if not json_mode:
                        print(f"  Warning: template error in post-command: {e}")

        # Save output if configured
        if task.output.save:
            try:
                save_path = Path(expand_all(task.output.save, ctx)).expanduser()
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_text(ctx.output)
                if not json_mode:
                    print(f"Output saved to: {save_path}")
            except Exception as e:
                if not json_mode:
                    print(f"Warning: Failed to save output: {e}")

        # Create PR if branch management is configured
        if work_branch and task.starting_ref:
            pr_url = _create_task_pr(project_path, task, work_branch, last_summary, json_mode)
            ctx.work_branch = work_branch
            if pr_url:
                ctx.pr_url = pr_url

        # Check if we should retry
        if last_status == "failed" and attempt < max_attempts:
            if not json_mode:
                print(f"Task failed, retrying in {task.retry_delay}s...")
            time.sleep(task.retry_delay)
            continue

        # Done (success or no more retries)
        break

    # Final result
    exit_code = status_to_exit_code(last_status)

    if json_mode:
        result_data = {
            "success": last_status == "complete",
            "status": last_status,
            "summary": last_summary,
            "attempt": ctx.attempt,
            "summary_file": ctx.summary_file,
        }
        if ctx.work_branch:
            result_data["work_branch"] = ctx.work_branch
        if ctx.pr_url:
            result_data["pr_url"] = ctx.pr_url
        _output_json(result_data)
    else:
        print(f"\nTask {task.name}: {last_status}")

    return exit_code


def cmd_task_list(args) -> int:
    """List tasks for a session/project."""
    from .tasks import list_tasks

    session = getattr(args, 'session', None)
    json_mode = getattr(args, 'json', False)

    # Find project path from session's working directory or cwd
    if session:
        project_path = _get_session_project_path(session)
    else:
        project_path = Path.cwd()

    if not project_path or not project_path.exists():
        return _output_result(False, json_mode, f"Project path not found: {project_path}")

    tasks = list_tasks(project_path)

    if json_mode:
        _output_json({"tasks": tasks, "project": str(project_path)})
        return 0

    if not tasks:
        print(f"No tasks defined in {project_path / '.agentwire.yml'}")
        return 0

    print(f"Tasks in {project_path.name}:\n")
    print(f"{'Name':<25} {'Mode':<10} {'Pre':<5} {'Post':<5} {'Retries':<8}")
    print("-" * 60)
    for t in tasks:
        pre = "Yes" if t["has_pre"] else "-"
        post = "Yes" if t["has_post"] else "-"
        mode = t.get("mode", "standard")
        print(f"{t['name']:<25} {mode:<10} {pre:<5} {post:<5} {t['retries']:<8}")

    return 0


def cmd_task_show(args) -> int:
    """Show task definition details."""
    from .tasks import TaskNotFound, TaskValidationError, load_task, validate_task

    task_arg = args.task  # format: session/task or just task
    json_mode = getattr(args, 'json', False)

    # Parse task argument
    if "/" in task_arg:
        session, task_name = task_arg.split("/", 1)
    else:
        session = None
        task_name = task_arg

    # Find project path from session's working directory or cwd
    if session:
        project_path = _get_session_project_path(session)
    else:
        project_path = Path.cwd()

    try:
        task = load_task(project_path, task_name)
    except (TaskNotFound, TaskValidationError) as e:
        return _output_result(False, json_mode, str(e))

    issues = validate_task(task)

    if json_mode:
        _output_json({
            "name": task.name,
            "prompt": task.prompt,
            "shell": task.shell,
            "retries": task.retries,
            "retry_delay": task.retry_delay,
            "idle_timeout": task.idle_timeout,
            "mode": task.mode,
            "max_iterations": task.max_iterations,
            "loop_review": task.loop_review,
            "loop_delay": task.loop_delay,
            "pre": [{"name": p.name, "cmd": p.cmd, "required": p.required, "validate": p.validate, "timeout": p.timeout} for p in task.pre],
            "on_task_end": task.on_task_end,
            "post": task.post,
            "output": {"capture": task.output.capture, "save": task.output.save},
            "validation_issues": issues,
        })
        return 0

    print(f"Task: {task.name}\n")
    print(f"Shell: {task.shell or '/bin/sh'}")
    print(f"Mode: {task.mode}")
    if task.mode == "loop":
        print(f"Max iterations: {task.max_iterations}")
        print(f"Loop review: {task.loop_review}")
        if task.loop_delay > 0:
            print(f"Loop delay: {task.loop_delay}s")
    print(f"Retries: {task.retries} (delay: {task.retry_delay}s)")
    print(f"Idle timeout: {task.idle_timeout}s")
    print()

    if task.pre:
        print("Pre-commands:")
        for p in task.pre:
            req = " (required)" if p.required else ""
            print(f"  {p.name}: {p.cmd}{req}")
        print()

    print("Prompt:")
    print(task.prompt[:200] + "..." if len(task.prompt) > 200 else task.prompt)
    print()

    if task.on_task_end:
        print("On task end:")
        print(task.on_task_end[:100] + "..." if len(task.on_task_end) > 100 else task.on_task_end)
        print()

    if task.post:
        print("Post-commands:")
        for cmd in task.post:
            print(f"  {cmd}")
        print()

    if task.output.save:
        print("Output:")
        print(f"  Save to: {task.output.save}")

    if issues:
        print(f"\nValidation issues: {', '.join(issues)}")

    return 0


def cmd_task_validate(args) -> int:
    """Validate task configuration."""
    from .tasks import TaskNotFound, TaskValidationError, load_task, validate_task

    task_arg = args.task
    json_mode = getattr(args, 'json', False)

    # Parse task argument
    if "/" in task_arg:
        session, task_name = task_arg.split("/", 1)
    else:
        session = None
        task_name = task_arg

    # Find project path from session's working directory or cwd
    if session:
        project_path = _get_session_project_path(session)
    else:
        project_path = Path.cwd()

    try:
        task = load_task(project_path, task_name)
    except (TaskNotFound, TaskValidationError) as e:
        return _output_result(False, json_mode, str(e))

    issues = validate_task(task)

    if json_mode:
        _output_json({
            "valid": len(issues) == 0,
            "issues": issues,
            "task": task_name,
        })
        return 0 if not issues else 1

    if issues:
        print(f"Task '{task_name}' has issues:")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    else:
        print(f"Task '{task_name}' is valid.")
        return 0


# =============================================================================
# Lock Management Commands
# =============================================================================


def cmd_lock_list(args) -> int:
    """List all locks with metadata."""
    from .locking import list_locks

    json_mode = getattr(args, 'json', False)
    locks = list_locks()

    if json_mode:
        _output_json({"locks": locks})
        return 0

    if not locks:
        print("No locks found.")
        return 0

    # Format output
    print(f"{'SESSION':<25} {'PID':<10} {'AGE':<12} {'STATUS'}")
    print("-" * 60)

    for lock in locks:
        session = lock["session"][:24]
        pid = str(lock["pid"]) if lock["pid"] else "-"
        age_seconds = lock["age_seconds"]

        # Format age
        if age_seconds < 60:
            age = f"{age_seconds}s"
        elif age_seconds < 3600:
            age = f"{age_seconds // 60}m {age_seconds % 60}s"
        elif age_seconds < 86400:
            hours = age_seconds // 3600
            mins = (age_seconds % 3600) // 60
            age = f"{hours}h {mins}m"
        else:
            days = age_seconds // 86400
            hours = (age_seconds % 86400) // 3600
            age = f"{days}d {hours}h"

        status = lock["status"]
        print(f"{session:<25} {pid:<10} {age:<12} {status}")

    return 0


def cmd_lock_clean(args) -> int:
    """Remove all stale locks."""
    from .locking import clean_stale_locks

    json_mode = getattr(args, 'json', False)
    dry_run = getattr(args, 'dry_run', False)

    removed = clean_stale_locks(dry_run=dry_run)

    if json_mode:
        _output_json({
            "removed": removed,
            "count": len(removed),
            "dry_run": dry_run,
        })
        return 0

    if not removed:
        print("No stale locks found.")
    elif dry_run:
        print(f"Would remove {len(removed)} stale lock(s): {', '.join(removed)}")
    else:
        print(f"Removed {len(removed)} stale lock(s): {', '.join(removed)}")

    return 0


def cmd_lock_remove(args) -> int:
    """Force-remove a specific lock."""
    from .locking import remove_lock

    session = args.session
    json_mode = getattr(args, 'json', False)

    removed = remove_lock(session)

    if json_mode:
        _output_json({
            "session": session,
            "removed": removed,
        })
        return 0 if removed else 1

    if removed:
        print(f"Removed lock: {session}")
        return 0
    else:
        print(f"No lock found for: {session}")
        return 1


def cmd_lock(args) -> int:
    """Lock command dispatcher - shows help if no subcommand."""
    # This will be called if no subcommand is provided
    # The help is printed in main() based on lock_command being None
    return 0


# =============================================================================
# Scheduler Commands
# =============================================================================


SCHEDULER_SESSION = "agentwire-scheduler"


def cmd_scheduler_start(args) -> int:
    """Start the scheduler daemon in a tmux session."""
    if not _check_tmux_installed():
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
    """Run the scheduler loop in the foreground (for tmux)."""
    from .scheduler import run_scheduler_loop

    run_scheduler_loop()
    return 0


def cmd_scheduler_stop(args) -> int:
    """Stop the scheduler daemon."""
    if not tmux_session_exists(SCHEDULER_SESSION):
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
        load_board,
        pick_next_task,
        read_events,
    )

    json_mode = getattr(args, 'json', False)
    running = tmux_session_exists(SCHEDULER_SESSION)
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

    status_str = "running" if running else "stopped"
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
    keep = {"task_completed", "task_failed", "gate_error"}
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
            # invisible) takes precedence; else the summary behind a bad status.
            detail = ""
            if r.get("last_gate_error"):
                detail = f"[gate-error] {r['last_gate_error']}"
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
    """Generate and open a live HTML dashboard as a portal artifact."""
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


class VersionAction(argparse.Action):
    """Custom version action that checks Python version and pip environment."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        # Print version
        print(f"agentwire {__version__}")
        print(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

        # Check version compatibility
        version_ok = check_python_version()
        env_ok = check_pip_environment()

        if version_ok and env_ok:
            print("\n✓ System is ready for AgentWire")
        else:
            print("\n⚠️  Please resolve the issues above before installing/running AgentWire")

        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser with every registered subcommand.

    Extracted from ``main()`` so the parser tree can be constructed without
    dispatching, which the CLI smoke tests rely on to enumerate subcommands
    and invoke their ``--help`` (see ``tests/unit/test_cli_smoke.py``).
    """
    parser = argparse.ArgumentParser(
        prog="agentwire",
        description="Multi-session voice web interface for AI coding agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Command Categories:
  Getting Started:
    init             Interactive setup wizard
    portal           Manage the web portal
    new              Create a new Claude Code session
    say              Speak text via TTS

  Sessions:
    list             List panes or sessions
    info             Get session information
    kill             Kill a session or pane
    spawn            Spawn a worker pane in current session
    worktree         Create a git worktree + session
    send             Send prompt to a session or pane
    output           Read session or pane output

  Voice:
    listen           Voice input recording
    voiceclone       Record and upload voice clones
    tts              Manage TTS server
    stt              Manage STT server

  Diagnostics:
    doctor           Auto-diagnose and fix common issues
    network          Network diagnostics and status
    safety           Damage control security commands
    hooks            Manage agentwire hook files

  Advanced:
    council          Multi-soul council operations
    scheduler        Manage the task scheduler
    ensure           Run named task with reliable session management
    limits           Usage-limit recovery management
"""
    )
    parser.add_argument(
        "--version",
        action=VersionAction,
        help="Show version and check system compatibility",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # === init command ===
    init_parser = subparsers.add_parser("init", help="Interactive setup wizard")
    init_parser.add_argument(
        "--assisted", action="store_true",
        help="Spawn the interactive Claude setup session at the end "
             "(default: end on the portal-URL next steps)"
    )
    init_parser.set_defaults(func=cmd_init)

    # === quo command ===
    from agentwire.channels.quo import cmd_quo
    quo_parser = subparsers.add_parser("quo", help="Send SMS via Quo (OpenPhone)")
    quo_parser.add_argument("--body", "-b", type=str, help="Message body (or pipe via stdin)")
    quo_parser.add_argument("--to", type=str, help="Recipient phone number (+E.164 format)")
    quo_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    quo_parser.set_defaults(func=cmd_quo)

    # === tunnels command group ===
    tunnels_parser = subparsers.add_parser("tunnels", help="Manage SSH tunnels for service routing")
    tunnels_subparsers = tunnels_parser.add_subparsers(dest="tunnels_command")

    # tunnels up
    tunnels_up = tunnels_subparsers.add_parser("up", help="Create all required tunnels")
    tunnels_up.set_defaults(func=cmd_tunnels_up)

    # tunnels down
    tunnels_down = tunnels_subparsers.add_parser("down", help="Tear down all tunnels")
    tunnels_down.set_defaults(func=cmd_tunnels_down)

    # tunnels status
    tunnels_status = tunnels_subparsers.add_parser("status", help="Show tunnel health")
    tunnels_status.set_defaults(func=cmd_tunnels_status)

    # tunnels check
    tunnels_check = tunnels_subparsers.add_parser("check", help="Verify tunnels are working")
    tunnels_check.set_defaults(func=cmd_tunnels_check)

    # === notify command (worker→parent) ===
    notify_cmd_parser = subparsers.add_parser("notify-parent", help="Notify parent session (worker→orchestrator)")
    notify_cmd_parser.add_argument("text", nargs="*", help="Notification message")
    notify_cmd_parser.add_argument("--to", type=str, metavar="SESSION", help="Target session (default: parent from .agentwire.yml)")
    notify_cmd_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress output")
    notify_cmd_parser.add_argument("--raw", action="store_true",
                                   help="Send the message verbatim (no [NOTIFY from ...] prefix)")
    notify_cmd_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_cmd_parser.set_defaults(func=cmd_notify_parent)

    # === open command (artifact windows) ===
    open_parser = subparsers.add_parser("open", help="Open a URL or local file as an artifact window in the portal")
    open_parser.add_argument("url", help="URL or filename to open (filenames served from ~/.agentwire/artifacts/)")
    open_parser.add_argument("--title", "-t", type=str, default="Artifact", help="Window title")
    open_parser.add_argument("--artifact-id", type=str, help="Unique window ID (auto-generated if omitted)")
    open_parser.add_argument("--json", action="store_true", help="Output JSON")
    open_parser.set_defaults(func=cmd_open)

    # === email command ===
    from agentwire.channels.email import cmd_email
    email_parser = subparsers.add_parser("email", help="Send branded email notification via Resend")
    email_parser.add_argument(
        "--to", action="append", default=None,
        help="Recipient email. Repeat or pass comma-separated for multiple recipients (default: from config).",
    )
    email_parser.add_argument("--subject", "-s", type=str, help="Email subject")
    email_parser.add_argument("--body", "-b", type=str, help="Email body - markdown supported (or pipe via stdin)")
    email_parser.add_argument("--attach", "-a", type=str, action="append", help="Attach file (can use multiple times)")
    email_parser.add_argument("--plain", action="store_true", help="Send plain text only (no HTML template)")
    email_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    email_parser.set_defaults(func=cmd_email)

    # === push command (#483) ===
    from agentwire.channels.push import cmd_push
    push_parser = subparsers.add_parser(
        "push", help="Web Push (VAPID) for the PWA — generate keys / check status"
    )
    push_sub = push_parser.add_subparsers(dest="push_cmd")
    push_sub.add_parser("keygen", help="Generate a VAPID keypair for ~/.agentwire/.env")
    push_sub.add_parser("status", help="Show push readiness + subscription count")
    push_parser.set_defaults(func=cmd_push)

    # === fetch command ===
    from agentwire.fetch import cmd_fetch
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch a URL via Jina Reader — handles JS-rendered pages, returns clean markdown.",
    )
    fetch_parser.add_argument("url", help="URL to fetch")
    fetch_parser.add_argument(
        "--limit", "-l", type=int, default=8000,
        help="Max characters to return (default: 8000, 0 = no limit)",
    )
    fetch_parser.set_defaults(func=cmd_fetch)

    # === notify command ===
    notify_parser = subparsers.add_parser("notify-event", help="Broadcast a portal lifecycle event (session/pane state change); usually called by tmux hooks")
    notify_parser.add_argument(
        "event",
        help="Event type: session_closed, session_created, pane_died, pane_created, "
             "client_attached, client_detached, session_renamed, pane_focused, window_activity"
    )
    notify_parser.add_argument("-s", "--session", help="Session name")
    notify_parser.add_argument("--pane", type=int, help="Pane index (for pane events)")
    notify_parser.add_argument("--pane-id", help="Pane ID from tmux (for pane events via hooks)")
    notify_parser.add_argument("--old-name", help="Old session name (for session_renamed)")
    notify_parser.add_argument("--new-name", help="New session name (for session_renamed)")
    notify_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_parser.set_defaults(func=cmd_notify)

    # notify-user: human-facing desktop toast (the CLI twin of MCP notify_user)
    notify_user_parser = subparsers.add_parser("notify-user", help="Show the human a desktop toast on the portal")
    notify_user_parser.add_argument("text", nargs="+", help="Toast text (supports a safe markdown subset: bold, links, line breaks)")
    notify_user_parser.add_argument("-s", "--session", help="Session this relates to (shown as a badge)")
    notify_user_parser.add_argument("--priority", default="normal", choices=["normal", "high"], help="Toast priority")
    notify_user_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_user_parser.set_defaults(func=cmd_notify_user)

    # research: Briefing Mode dropbox path resolver
    research_parser = subparsers.add_parser("research", help="Resolve the Briefing Mode research dropbox path for a session")
    research_sub = research_parser.add_subparsers(dest="research_command")
    for _verb, _help in (("dir", "Print the dropbox path (not created)"),
                         ("ensure", "Create + print the dropbox path")):
        _rp = research_sub.add_parser(_verb, help=_help)
        _rp.add_argument("-s", "--session", default=None, help="Anchor session (default: current)")
        _rp.add_argument("--json", action="store_true", help="Output as JSON")
        _rp.set_defaults(func=cmd_research)
    research_parser.add_argument("-s", "--session", default=None, help="Anchor session (default: current)")
    research_parser.add_argument("--json", action="store_true", help="Output as JSON")
    research_parser.set_defaults(func=cmd_research)

    # wiki: deterministic mechanical ops for the LLM wiki
    from . import wiki as _wiki_mod
    wiki_parser = subparsers.add_parser("wiki", help="Deterministic mechanical ops for the LLM wiki")
    wiki_parser.add_argument("--root", type=Path, default=None,
                             help=f"Wiki root (default: {_wiki_mod.DEFAULT_WIKI_ROOT})")
    wiki_sub = wiki_parser.add_subparsers(dest="wiki_command")

    _ws = wiki_sub.add_parser("status", help="Page counts, unprocessed raw, lint summary")
    _ws.add_argument("--json", action="store_true", help="Output as JSON")
    _ws.set_defaults(func=cmd_wiki)

    _wq = wiki_sub.add_parser("query", help="Ranked deterministic search (caller synthesizes)")
    _wq.add_argument("query")
    _wq.add_argument("--limit", type=int, default=10)
    _wq.add_argument("--json", action="store_true", help="Output as JSON")
    _wq.set_defaults(func=cmd_wiki)

    _wl = wiki_sub.add_parser("lint", help="Structural + ground-truth checks (never auto-fixes)")
    _wl.add_argument("--strict", action="store_true", help="Exit 1 when issues found")
    _wl.add_argument("--json", action="store_true", help="Output as JSON")
    _wl.set_defaults(func=cmd_wiki)

    _wn = wiki_sub.add_parser("new", help="Scaffold wiki/<category>/<name>.md")
    _wn.add_argument("category", choices=_wiki_mod.CATEGORIES)
    _wn.add_argument("name")
    _wn.add_argument("--title", default=None)
    _wn.add_argument("--json", action="store_true", help="Output as JSON")
    _wn.set_defaults(func=cmd_wiki)

    _wd = wiki_sub.add_parser("done", help="Archive raw/<f> → raw/processed/<f>")
    _wd.add_argument("rawfile")
    _wd.add_argument("--json", action="store_true", help="Output as JSON")
    _wd.set_defaults(func=cmd_wiki)

    wiki_parser.set_defaults(func=cmd_wiki)

    # === dev command ===
    dev_parser = subparsers.add_parser(
        "dev", help="Start/attach to dev agentwire session"
    )
    dev_parser.add_argument("--no-soul", dest="no_soul", action="store_true", help="Skip soul personality role injection for this session")
    dev_parser.set_defaults(func=cmd_dev)

    up_parser = subparsers.add_parser(
        "up", help="Boot all services (portal, TTS, STT, scheduler, custom) then the dev session"
    )
    up_parser.add_argument("--dev", action="store_true", help="Run portal from source (uv run)")
    up_parser.add_argument("--no-tts", action="store_true", help="Skip starting the TTS server")
    up_parser.add_argument("--no-stt", action="store_true", help="Skip starting the STT server")
    up_parser.add_argument("--config", type=Path, default=None, help="Path to config file")
    up_parser.set_defaults(func=cmd_up)

    # === listen command group ===
    listen_parser = subparsers.add_parser("listen", help="Voice input recording")
    listen_parser.add_argument(
        "--session", "-s", type=str, default="agentwire",
        help="Target session (default: agentwire)"
    )
    listen_parser.add_argument(
        "--no-prompt", action="store_true",
        help="Don't prepend voice prompt hint"
    )
    listen_subparsers = listen_parser.add_subparsers(dest="listen_command")

    # listen start
    listen_start = listen_subparsers.add_parser("start", help="Start recording")
    listen_start.set_defaults(func=cmd_listen_start)

    # listen stop
    listen_stop = listen_subparsers.add_parser("stop", help="Stop and send")
    listen_stop.add_argument("--session", "-s", type=str, help="Target session")
    listen_stop.add_argument("--no-prompt", action="store_true")
    listen_stop.add_argument("--type", action="store_true", help="Type at cursor instead of sending to session")
    listen_stop.add_argument("--stdout", action="store_true", help="Print the raw transcript to stdout (no paste, no tmux send)")
    listen_stop.set_defaults(func=cmd_listen_stop)

    # listen cancel
    listen_cancel = listen_subparsers.add_parser("cancel", help="Cancel recording")
    listen_cancel.set_defaults(func=cmd_listen_cancel)

    # Default listen (no subcommand) = toggle
    listen_parser.set_defaults(func=cmd_listen_toggle)

    # === machine command group ===
    machine_parser = subparsers.add_parser("machine", help="Manage remote machines")
    machine_subparsers = machine_parser.add_subparsers(dest="machine_command")

    # machine list
    machine_list = machine_subparsers.add_parser("list", help="List registered machines")
    machine_list.add_argument("--json", action="store_true", help="Output JSON")
    machine_list.set_defaults(func=cmd_machine_list)

    # machine add <id>
    machine_add = machine_subparsers.add_parser(
        "add", help="Add a machine to the network"
    )
    machine_add.add_argument("machine_id", help="Machine ID (used in session names)")
    machine_add.add_argument("--host", help="SSH host (defaults to machine_id)")
    machine_add.add_argument("--user", help="SSH user")
    machine_add.add_argument("--projects-dir", dest="projects_dir", help="Projects directory on remote")
    machine_add.set_defaults(func=cmd_machine_add)

    # machine remove <id>
    machine_remove = machine_subparsers.add_parser(
        "remove", help="Remove a machine from the network"
    )
    machine_remove.add_argument("machine_id", help="Machine ID to remove")
    machine_remove.set_defaults(func=cmd_machine_remove)

    # === history command group ===
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

    # === handoff command group ===
    handoff_parser = subparsers.add_parser(
        "handoff",
        help="Shareable conversation bundles (ai-handoff.md + show-the-story.html)",
    )
    handoff_subparsers = handoff_parser.add_subparsers(dest="handoff_command")

    handoff_init_parser = handoff_subparsers.add_parser(
        "init",
        help="Create a bundle dir + pre-filled ai-handoff.md template",
    )
    handoff_init_parser.add_argument(
        "--title", help="Short title hint, used in the bundle slug"
    )
    handoff_init_parser.add_argument(
        "--output-dir", help="Override artifacts dir (default: ~/.agentwire/artifacts)"
    )
    handoff_init_parser.add_argument(
        "--json", action="store_true", help="JSON output"
    )
    handoff_init_parser.set_defaults(func=cmd_handoff_init)

    handoff_render_parser = handoff_subparsers.add_parser(
        "render",
        help="Render show-the-story.html from an existing ai-handoff.md",
    )
    handoff_render_parser.add_argument(
        "path", help="Bundle dir or path to ai-handoff.md"
    )
    handoff_render_parser.add_argument(
        "--story", action="store_true", default=True,
        help="Render show-the-story.html (default: on)",
    )
    handoff_render_parser.add_argument(
        "--no-story", dest="story", action="store_false",
        help="Skip HTML render (only validates the markdown)",
    )
    handoff_render_parser.add_argument(
        "--json", action="store_true", help="JSON output"
    )
    handoff_render_parser.set_defaults(func=cmd_handoff_render)

    handoff_list_parser = handoff_subparsers.add_parser(
        "list", help="List past handoff bundles"
    )
    handoff_list_parser.add_argument(
        "--json", action="store_true", help="JSON output"
    )
    handoff_list_parser.set_defaults(func=cmd_handoff_list)

    # === roles command group ===
    roles_parser = subparsers.add_parser(
        "roles", help="Manage composable roles"
    )
    roles_subparsers = roles_parser.add_subparsers(dest="roles_command")

    # roles list
    roles_list = roles_subparsers.add_parser("list", help="List available roles")
    roles_list.add_argument("--json", action="store_true", help="Output as JSON")
    roles_list.set_defaults(func=cmd_roles_list)

    # roles show <name>
    roles_show = roles_subparsers.add_parser("show", help="Show role details")
    roles_show.add_argument("name", help="Role name")
    roles_show.add_argument("--json", action="store_true", help="Output as JSON")
    roles_show.set_defaults(func=cmd_roles_show)

    # === projects command group ===
    projects_parser = subparsers.add_parser(
        "projects", help="Discover and list projects"
    )
    projects_subparsers = projects_parser.add_subparsers(dest="projects_command")

    # projects list
    projects_list = projects_subparsers.add_parser("list", help="List discovered projects")
    projects_list.add_argument("--machine", help="Filter by machine ID (e.g., 'local', 'mac-studio')")
    projects_list.add_argument("--json", action="store_true", help="Output as JSON")
    projects_list.set_defaults(func=cmd_projects_list)

    # projects create
    projects_create = projects_subparsers.add_parser(
        "create",
        help="Create a new local project under projects.dir with a default .agentwire.yml",
    )
    projects_create.add_argument("name", help="Project name (directory name under projects.dir)")
    projects_create.add_argument(
        "--from", dest="from_url", help="Clone from this git URL instead of creating an empty directory"
    )
    projects_create.add_argument(
        "--git-init", action="store_true", help="Run 'git init' in the new project (ignored when --from is given)"
    )
    projects_create.add_argument("--json", action="store_true", help="Output as JSON")
    projects_create.set_defaults(func=cmd_projects_create)

    # === hooks command group ===
    hooks_parser = subparsers.add_parser(
        "hooks", help="Manage agentwire hook files (permission, idle handler, queue processor)"
    )
    hooks_subparsers = hooks_parser.add_subparsers(dest="hooks_command")

    # hooks install
    hooks_install = hooks_subparsers.add_parser(
        "install", help="Install/refresh agentwire hook files and slash commands"
    )
    hooks_install.add_argument(
        "--force", "-f", action="store_true", help="Reinstall even when already current"
    )
    hooks_install.add_argument(
        "--copy", action="store_true", help="Copy files instead of symlinking"
    )
    hooks_install.set_defaults(func=cmd_hooks_install)

    # hooks uninstall
    hooks_uninstall = hooks_subparsers.add_parser(
        "uninstall", help="Remove agentwire hook files and their registration"
    )
    hooks_uninstall.set_defaults(func=cmd_hooks_uninstall)

    # hooks status
    hooks_status = hooks_subparsers.add_parser(
        "status", help="Check hook installation status"
    )
    hooks_status.set_defaults(func=cmd_hooks_status)

    # === network command group ===
    network_parser = subparsers.add_parser(
        "network", help="Network diagnostics and status"
    )
    network_subparsers = network_parser.add_subparsers(dest="network_command")

    # network status
    network_status = network_subparsers.add_parser(
        "status", help="Show complete network health at a glance"
    )
    network_status.set_defaults(func=cmd_network_status)

    # === safety command group ===
    safety_parser = subparsers.add_parser(
        "safety", help="Damage control security commands"
    )
    safety_subparsers = safety_parser.add_subparsers(dest="safety_command")

    # safety check <command>
    safety_check = safety_subparsers.add_parser(
        "check", help="Test if a command would be blocked/allowed"
    )
    safety_check.add_argument("command", help="Command to test")
    safety_check.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )
    safety_check.set_defaults(func=cmd_safety_check)

    # safety status
    safety_status = safety_subparsers.add_parser(
        "status", help="Show safety status and pattern counts"
    )
    safety_status.set_defaults(func=cmd_safety_status)

    # safety notify-unattended-block (hook-invoked, not for humans)
    safety_notify = safety_subparsers.add_parser(
        "notify-unattended-block",
        help="Email the owner that an unattended action was blocked (hook-internal)",
    )
    safety_notify.add_argument("--reason", default="", help="Why the command was blocked")
    safety_notify.add_argument("--rule-id", dest="rule_id", default="", help="Matched rule id")
    safety_notify.add_argument("--command", default="", help="The blocked command")
    safety_notify.set_defaults(func=cmd_safety_notify_unattended_block)

    # safety logs
    safety_logs = safety_subparsers.add_parser(
        "logs", help="Query audit logs"
    )
    safety_logs.add_argument(
        "--tail", "-n", type=int, help="Show last N entries"
    )
    safety_logs.add_argument(
        "--session", "-s", help="Filter by session ID"
    )
    safety_logs.add_argument(
        "--today", action="store_true", help="Show only today's logs"
    )
    safety_logs.add_argument(
        "--pattern", "-p", help="Filter by pattern (regex or substring)"
    )
    safety_logs.set_defaults(func=cmd_safety_logs)

    # safety install
    safety_install = safety_subparsers.add_parser(
        "install", help="Install/heal damage control hooks, rules, and matchers"
    )
    safety_install.add_argument(
        "-y", "--yes", action="store_true",
        help="Non-interactive, drift-aware heal (install missing + update stale "
             "owned hooks; never clobbers existing rules)",
    )
    safety_install.set_defaults(func=cmd_safety_install)

    # safety tooldefs
    safety_tooldefs = safety_subparsers.add_parser(
        "tooldefs", help="Browse tool definitions"
    )
    tooldefs_subparsers = safety_tooldefs.add_subparsers(dest="tooldefs_command")

    tooldefs_list = tooldefs_subparsers.add_parser("list", help="List available tooldefs")
    tooldefs_list.set_defaults(func=cmd_safety_tooldefs_list)

    tooldefs_show = tooldefs_subparsers.add_parser("show", help="Show tooldef for a tool")
    tooldefs_show.add_argument("tool", help="Tool name (e.g. git, gh, docker)")
    tooldefs_show.set_defaults(func=cmd_safety_tooldefs_show)

    # === doctor command (top-level) ===
    doctor_parser = subparsers.add_parser(
        "doctor", help="Auto-diagnose and fix common issues"
    )
    doctor_parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without making changes"
    )
    doctor_parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Auto-confirm all fixes without prompting"
    )
    doctor_parser.add_argument(
        "--voice", action="store_true",
        help="Run only the voice loop (push-to-talk) preflight — fast, no SSH waits"
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    # === generate-certs (top-level shortcut) ===
    certs_parser = subparsers.add_parser(
        "generate-certs", help="Generate SSL certificates"
    )
    certs_parser.set_defaults(func=cmd_generate_certs)

    # === rebuild command ===
    rebuild_parser = subparsers.add_parser(
        "rebuild", help="Clear uv cache and reinstall from source (for development)"
    )
    rebuild_parser.add_argument(
        "--force", action="store_true",
        help="Rebuild even when the local checkout is behind origin/main",
    )
    rebuild_parser.set_defaults(func=cmd_rebuild)

    # === uninstall command ===
    uninstall_parser = subparsers.add_parser(
        "uninstall", help="Clear uv cache and uninstall the tool"
    )
    uninstall_parser.set_defaults(func=cmd_uninstall)

    # === mcp command ===
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Run MCP server for external agent integration",
        description="Expose AgentWire as an MCP server for tools like MoltBot, Claude Desktop, etc.",
    )
    mcp_parser.set_defaults(func=cmd_mcp)

    # === ensure command (scheduled workloads) ===
    ensure_parser = subparsers.add_parser(
        "ensure",
        help="Run named task with reliable session management",
        description="Execute a task from .agentwire.yml with locking, retries, and completion detection.",
    )
    ensure_parser.add_argument("-s", "--session", required=True, help="Target session name")
    ensure_parser.add_argument("-p", "--project", help="Project path containing .agentwire.yml (defaults to ~/projects/{session})")
    ensure_parser.add_argument("--task", required=True, help="Task name from .agentwire.yml")
    ensure_parser.add_argument("--dry-run", action="store_true", help="Show what would execute without running")
    ensure_parser.add_argument("--wait-lock", action="store_true", help="Wait for lock instead of failing if locked")
    ensure_parser.add_argument("--lock-timeout", type=int, default=60, help="Max time to wait for lock (default: 60s)")
    ensure_parser.add_argument("--skip-if-locked", action="store_true", help="Exit 0 silently if session is locked (for cron use cases)")
    ensure_parser.add_argument("--json", action="store_true", help="Output JSON")
    ensure_parser.set_defaults(func=cmd_ensure)

    # === task command group ===
    task_parser = subparsers.add_parser(
        "task",
        help="Manage scheduled tasks",
        description="List, show, and validate tasks defined in .agentwire.yml.",
    )
    task_subparsers = task_parser.add_subparsers(dest="task_command")

    # task list
    task_list = task_subparsers.add_parser("list", help="List tasks for session/project")
    task_list.add_argument("session", nargs="?", help="Session name (default: current directory)")
    task_list.add_argument("--json", action="store_true", help="Output JSON")
    task_list.set_defaults(func=cmd_task_list)

    # task show
    task_show = task_subparsers.add_parser("show", help="Show task definition details")
    task_show.add_argument("task", help="Task name (session/task or just task)")
    task_show.add_argument("--json", action="store_true", help="Output JSON")
    task_show.set_defaults(func=cmd_task_show)

    # task validate
    task_validate = task_subparsers.add_parser("validate", help="Validate task configuration")
    task_validate.add_argument("task", help="Task name (session/task or just task)")
    task_validate.add_argument("--json", action="store_true", help="Output JSON")
    task_validate.set_defaults(func=cmd_task_validate)

    # === lock command group ===
    lock_parser = subparsers.add_parser(
        "lock",
        help="Manage session locks",
        description="List, clean, and remove session locks.",
    )
    lock_subparsers = lock_parser.add_subparsers(dest="lock_command")
    lock_parser.set_defaults(func=cmd_lock)

    # lock list
    lock_list_parser = lock_subparsers.add_parser("list", help="List all locks")
    lock_list_parser.add_argument("--json", action="store_true", help="Output JSON")
    lock_list_parser.set_defaults(func=cmd_lock_list)

    # lock clean
    lock_clean_parser = lock_subparsers.add_parser("clean", help="Remove stale locks")
    lock_clean_parser.add_argument("--dry-run", action="store_true", help="Show what would be removed")
    lock_clean_parser.add_argument("--json", action="store_true", help="Output JSON")
    lock_clean_parser.set_defaults(func=cmd_lock_clean)

    # lock remove
    lock_remove_parser = lock_subparsers.add_parser("remove", help="Force-remove a lock")
    lock_remove_parser.add_argument("session", help="Session name")
    lock_remove_parser.add_argument("--json", action="store_true", help="Output JSON")
    lock_remove_parser.set_defaults(func=cmd_lock_remove)

    # === scratchpad command group ===
    scratchpad_parser = subparsers.add_parser(
        "scratchpad",
        help="Shared scratch pad notes (portal drawer; agents add via MCP)",
        description=(
            "Persistent notes in ~/.agentwire/scratchpad.json, shared across all "
            "portal clients (the slide-in drawer, Alt+N) and agents. Mutations "
            "ping a running portal so open drawers refresh live."
        ),
    )
    scratchpad_subparsers = scratchpad_parser.add_subparsers(dest="scratchpad_command")

    scratchpad_list_parser = scratchpad_subparsers.add_parser("list", help="List notes")
    scratchpad_list_parser.add_argument("--json", action="store_true", help="Output JSON")
    scratchpad_list_parser.set_defaults(func=cmd_scratchpad_list)

    scratchpad_add_parser = scratchpad_subparsers.add_parser("add", help="Add a note")
    scratchpad_add_parser.add_argument("text", help="Note text")
    scratchpad_add_parser.add_argument("--source", help="Provenance label (e.g. session name)")
    scratchpad_add_parser.add_argument("--json", action="store_true", help="Output JSON")
    scratchpad_add_parser.set_defaults(func=cmd_scratchpad_add)

    scratchpad_remove_parser = scratchpad_subparsers.add_parser("remove", help="Remove a note")
    scratchpad_remove_parser.add_argument("id", help="Note id (see list)")
    scratchpad_remove_parser.add_argument("--json", action="store_true", help="Output JSON")
    scratchpad_remove_parser.set_defaults(func=cmd_scratchpad_remove)

    scratchpad_clear_parser = scratchpad_subparsers.add_parser("clear", help="Remove all notes")
    scratchpad_clear_parser.add_argument("--json", action="store_true", help="Output JSON")
    scratchpad_clear_parser.set_defaults(func=cmd_scratchpad_clear)

    # === services command group ===
    services_parser = subparsers.add_parser(
        "services",
        help="Manage user-defined services (long-running registered sessions)",
        description=(
            "Custom services are long-running agentwire sessions registered in "
            "services.custom in ~/.agentwire/config.yaml. They autostart on portal "
            "launch and `agentwire up`, and the portal watchdog health-checks them "
            "(restart: never | on-failure | always, with backoff). "
            "The notifications bridge session is a built-in registry entry."
        ),
    )
    services_subparsers = services_parser.add_subparsers(dest="services_command")

    services_list_parser = services_subparsers.add_parser("list", help="List registered services")
    services_list_parser.add_argument("--json", action="store_true", help="Output JSON")
    services_list_parser.set_defaults(func=cmd_services_list)

    services_status_parser = services_subparsers.add_parser(
        "status", help="Run healthchecks and report per-service status"
    )
    services_status_parser.add_argument("name", nargs="?", help="Service name (default: all)")
    services_status_parser.add_argument("--json", action="store_true", help="Output JSON")
    services_status_parser.set_defaults(func=cmd_services_status)

    services_up_parser = services_subparsers.add_parser(
        "up", help="Start a service (clears 'down' state)"
    )
    services_up_parser.add_argument("name", nargs="?", help="Service name")
    services_up_parser.add_argument("--all", action="store_true",
                                    help="Start all autostart services (skips downed ones)")
    services_up_parser.add_argument("--json", action="store_true", help="Output JSON")
    services_up_parser.set_defaults(func=cmd_services_up)

    services_down_parser = services_subparsers.add_parser(
        "down", help="Stop a service and keep it stopped"
    )
    services_down_parser.add_argument("name", help="Service name")
    services_down_parser.add_argument("--json", action="store_true", help="Output JSON")
    services_down_parser.set_defaults(func=cmd_services_down)

    # === scheduler command group ===
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
    sched_dashboard.add_argument("--no-open", action="store_true", help="Generate HTML without opening")
    sched_dashboard.set_defaults(func=cmd_scheduler_dashboard)

    # scheduler report
    sched_report = scheduler_subparsers.add_parser("report", help="Generate morning report of recent task runs")
    sched_report.add_argument("--since", default="8h", metavar="DURATION", help="Time window (e.g. 8h, 12h, 1d) default: 8h")
    sched_report.add_argument("--artifact", action="store_true", help="Open report as portal artifact")
    sched_report.add_argument("--json", action="store_true", help="Output JSON")
    sched_report.set_defaults(func=cmd_scheduler_report)

    # === Extracted command groups (each registrar owns its own subparser) ===
    # Phase 1 of #495 appends one entry here per extracted domain.
    #   - limits: usage-limit recovery (detect dialog, park, auto-resume)
    #   - diff:   structured git diff for the mobile Review window
    #   - prompts: prompt routing (rides the limits watchdog)
    #   - msg:    polite agent-to-agent inbox (rides the watchdog)
    from . import diff_cli, limits_cli, msg_cli, pane_cli, prompts_cli, send_cli  # noqa: I001  # session_cli kept on its own line below to minimize Phase 1 #495 merge conflicts
    from . import session_cli
    from . import channels_cli, portal_cli, tts_cli
    from .council import cli as council_cli

    _REGISTRARS = [  # noqa: N806  # registry constant; Phase 1 of #495 appends here
        diff_cli.register_diff_parser,
        prompts_cli.register_prompts_parser,
        msg_cli.register_msg_parser,
        limits_cli.register_limits_parser,
        pane_cli.register_pane_parser,
        send_cli.register_send_parser,
        session_cli.register_session_parser,
        portal_cli.register_portal_parser,
        tts_cli.register_tts_parser,
        channels_cli.register_channels_parser,
    ]
    for _reg in _REGISTRARS:
        _reg(subparsers)

    # === council command group ===
    council_parser = subparsers.add_parser(
        "council",
        help="Multi-soul council: fan a prompt out to lens sessions, synthesize",
        description=(
            "An agentwire-council orchestrator session fans prompts out to a "
            "roster of lens souls (brain, conscience, gut, critic, historian, "
            "devils-advocate), collects their takes through a file inbox, and "
            "synthesizes. See docs/wiki/council.md."
        ),
    )
    council_subparsers = council_parser.add_subparsers(dest="council_command")

    # Targeting is shared: --name picks the sitting; absent, the cwd-repo-slug
    # if it matches a live sitting, else the sole live sitting, else error.
    _name_help = "Sitting name (default: cwd-repo-slug / sole live sitting)"

    # council start
    c_start = council_subparsers.add_parser(
        "start", help="Start a sitting: orchestrator + all soul sessions"
    )
    c_start.add_argument(
        "--name", help="Sitting name (default: cwd-repo-slug)"
    )
    c_start.add_argument(
        "--roster", help="Comma-separated lens names (default: full bundled roster)"
    )
    c_start.add_argument("--type", help="Session type (default: claude-bypass)")
    c_start.add_argument("--model", help="Model override for all council sessions")
    c_start.add_argument(
        "--force", action="store_true", help="Tear down a live sitting first"
    )
    c_start.add_argument("--json", action="store_true", help="Output JSON")
    c_start.set_defaults(func=council_cli.cmd_council_start)

    # council stop
    c_stop = council_subparsers.add_parser(
        "stop", help="Kill the sitting's sessions (prompt history kept)"
    )
    c_stop.add_argument("--name", help=_name_help)
    c_stop.add_argument("--json", action="store_true", help="Output JSON")
    c_stop.set_defaults(func=council_cli.cmd_council_stop)

    # council status
    c_status = council_subparsers.add_parser(
        "status", help="Sitting state, session liveness, open prompts"
    )
    c_status.add_argument("--name", help=_name_help)
    c_status.add_argument("--json", action="store_true", help="Output JSON")
    c_status.set_defaults(func=council_cli.cmd_council_status)

    # council list
    c_list = council_subparsers.add_parser(
        "list", help="Every known sitting: name, cwd, age, live sessions, prompts"
    )
    c_list.add_argument("--json", action="store_true", help="Output JSON")
    c_list.set_defaults(func=council_cli.cmd_council_list)

    # council ask
    c_ask = council_subparsers.add_parser(
        "ask", help="Fan a prompt out to every soul in the sitting"
    )
    c_ask.add_argument("prompt", nargs="?", help="Prompt text (or --file / stdin)")
    c_ask.add_argument("--name", help=_name_help)
    c_ask.add_argument("--file", help="Read prompt text from a file")
    c_ask.add_argument("--json", action="store_true", help="Output JSON")
    c_ask.set_defaults(func=council_cli.cmd_council_ask)

    # council collect
    c_collect = council_subparsers.add_parser(
        "collect", help="Wait for every soul's take/ack/pass (or timeout)"
    )
    c_collect.add_argument("--name", help=_name_help)
    c_collect.add_argument("--prompt", type=int, help="Prompt id (default: latest)")
    c_collect.add_argument(
        "--timeout", type=float, default=120, help="Soft timeout in seconds (default: 120)"
    )
    c_collect.add_argument(
        "--no-wait", action="store_true", help="Snapshot once, don't block"
    )
    c_collect.add_argument("--json", action="store_true", help="Output JSON")
    c_collect.set_defaults(func=council_cli.cmd_council_collect)

    # council reply (run by souls)
    c_reply = council_subparsers.add_parser(
        "reply", help="File a soul's reply: --take / --ack / --pass"
    )
    c_reply.add_argument("--name", help=_name_help)
    c_reply.add_argument("--prompt", type=int, help="Prompt id (default: latest)")
    c_reply.add_argument(
        "--take", action="store_true", help="Substantive take (text required)"
    )
    c_reply.add_argument(
        "--ack", action="store_true", help="Researching — follow-up coming"
    )
    c_reply.add_argument(
        "--pass", action="store_true", help="Nothing to add through this lens"
    )
    c_reply.add_argument("--soul", help="Lens name (default: inferred from session)")
    c_reply.add_argument("--text", help="Reply text")
    c_reply.add_argument("--file", help="Read reply text from a file")
    c_reply.add_argument("--json", action="store_true", help="Output JSON")
    c_reply.set_defaults(func=council_cli.cmd_council_reply)

    return parser


def _find_subparser(parser: argparse.ArgumentParser, *names: str):
    """Walk the subparser tree by command name(s); return the parser or None."""
    current = parser
    for name in names:
        sub = None
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                sub = action.choices.get(name)
                break
        if sub is None:
            return None
        current = sub
    return current


# Command groups whose bare invocation (no subcommand) prints group help.
_GROUP_COMMANDS = [
    "portal", "tts", "stt", "tunnels", "machine", "history", "handoff",
    "wiki", "hooks", "projects", "safety", "network", "listen",
    "voiceclone", "roles", "task", "lock", "scheduler", "council", "limits",
]


def main() -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    if (
        args.command in _GROUP_COMMANDS
        and getattr(args, f"{args.command}_command", None) is None
    ):
        _find_subparser(parser, args.command).print_help()
        return 0

    if (
        args.command == "safety"
        and getattr(args, "safety_command", None) == "tooldefs"
        and getattr(args, "tooldefs_command", None) is None
    ):
        _find_subparser(parser, "safety", "tooldefs").print_help()
        return 0

    if hasattr(args, "func"):
        return args.func(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
