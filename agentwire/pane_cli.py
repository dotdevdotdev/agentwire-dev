"""CLI for pane/window operations — ``agentwire list/info/output/kill/spawn/
split/detach/jump/resize``.

These commands manage tmux sessions and the worker panes inside them. Shared
stateless helpers come from :mod:`agentwire.core`; the domain-private helpers
(pane hooks, pane-0 state probing, graceful-exit policy) live here.

Note: this is distinct from :mod:`agentwire.pane_manager`, the lower-level
tmux pane primitives these commands call into.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import pane_manager
from .core import (
    CONFIG_DIR,
    _add_posture_flag,
    _check_tmux_installed,
    _display_parent,
    _get_agentwire_path,
    _get_all_machines,
    _get_machine_config,
    _notify_portal_sessions_changed,
    _output_json,
    _output_result,
    _parse_session_target,
    _resolve_session_type_from_args,
    _run_remote,
    build_agent_command,
    inject_session_env,
    load_config,
    parse_env_args,
    tmux_session_exists,
)
from .project_config import load_project_config
from .roles import inject_soul, load_roles, resolve_roles


def _install_pane_hooks(session_name: str, pane_index: int) -> None:
    """Install tmux hooks to notify portal of pane state changes.

    Installs:
    - after-kill-pane: Fires when a pane is killed
    - pane-focus-in: Fires when pane focus changes (for multi-pane sessions)

    Uses run-shell -b for background execution to not block tmux.
    """
    agentwire_path = _get_agentwire_path()

    # Check existing hooks
    result = subprocess.run(
        ["tmux", "show-hooks", "-t", session_name],
        capture_output=True,
        text=True,
    )
    existing = result.stdout

    # Install after-kill-pane hook on the session
    # Note: #{hook_pane} may be empty when pane is already dead, so we just notify
    # without pane-id and let the portal refresh its pane list
    # Use || true to suppress error display in tmux
    if "after-kill-pane" not in existing:
        hook_cmd = f'run-shell -b "{agentwire_path} notify-event pane_died -s {session_name} >/dev/null 2>&1 || true"'
        subprocess.run(
            ["tmux", "set-hook", "-t", session_name, "after-kill-pane", hook_cmd],
            capture_output=True,
        )

    # Install pane-focus-in hook for active pane tracking
    # This fires when a pane gains focus within the session
    # Use || true to suppress error display in tmux
    if "pane-focus-in" not in existing:
        hook_cmd = f'run-shell -b "{agentwire_path} notify-event pane_focused -s {session_name} --pane-id #{{pane_id}} >/dev/null 2>&1 || true"'
        subprocess.run(
            ["tmux", "set-hook", "-t", session_name, "pane-focus-in", hook_cmd],
            capture_output=True,
        )


def _get_session_config_from_path(path: str) -> dict:
    """Read session config from .agentwire.yml in the given path.

    Returns:
        Dict with 'type' and 'roles' keys (values may be None/empty).
    """
    import yaml

    if not path:
        return {"type": None, "roles": []}

    yml_path = Path(path) / ".agentwire.yml"
    if yml_path.exists():
        try:
            with open(yml_path) as f:
                config = yaml.safe_load(f) or {}
                return {
                    "type": config.get("type"),
                    "roles": config.get("roles", []) or [],
                }
        except Exception:
            pass
    return {"type": None, "roles": []}


def _get_session_type_from_path(path: str) -> str | None:
    """Read session type from .agentwire.yml in the given path."""
    return _get_session_config_from_path(path).get("type")


def _get_remote_session_type(machine_id: str, path: str) -> str | None:
    """Read session type from .agentwire.yml on a remote machine.

    Returns:
        Session type (e.g., 'bare', 'claude-bypass') or None
    """
    import yaml

    if not path or not machine_id:
        return None

    cmd = f"cat {path}/.agentwire.yml 2>/dev/null || echo ''"
    result = _run_remote(machine_id, cmd)

    if result.returncode == 0 and result.stdout.strip():
        try:
            config = yaml.safe_load(result.stdout) or {}
            return config.get("type")
        except Exception:
            pass
    return None


def list_local_sessions(show_context: bool = False) -> list[dict]:
    """List local tmux sessions in-process (no CLI subprocess).

    Single source of truth for local session listing — used by ``cmd_list``
    and imported directly by the portal server (#627) so the monitor loop and
    HTTP routes don't spawn a fresh interpreter per tick/request.
    """
    from .usage_limit import is_parked as usage_limit_parked

    sessions: list[dict] = []
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}:#{session_windows}:#{pane_current_path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return sessions

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        path = parts[2] if len(parts) > 2 else ""
        cfg = _get_session_config_from_path(path)
        session_info = {
            "name": parts[0],
            "windows": int(parts[1]) if parts[1].isdigit() else 1,
            "path": path,
            "machine": None,
            "type": cfg.get("type"),
            "roles": cfg.get("roles", []),
            "parent": _display_parent(parts[0], path),
        }
        if usage_limit_parked(parts[0]):
            session_info["usage_limit"] = True
        if show_context:
            from .session_context import session_context as _sctx
            ctx = _sctx(parts[0])
            session_info["context"] = {
                "is_agent": ctx.is_agent,
                "remaining_pct": ctx.remaining_pct,
                "model": ctx.model,
                "flagged": ctx.flagged,
                "note": ctx.note,
            }
        sessions.append(session_info)
    return sessions


def list_remote_sessions(machine_filter: str | None = None) -> dict[str, list[dict]]:
    """List remote tmux sessions in-process, grouped by machine.

    Returns ``{machine_id: [session_info, ...]}`` containing ONLY machines
    that answered over SSH — an unreachable machine is absent from the dict
    (vs. present with an empty list when reachable but session-less), so
    callers can tell "machine down" from "no sessions" (#629 eviction guard).
    Session names carry the ``@machine`` suffix.
    """
    remote_by_machine: dict[str, list[dict]] = {}
    for machine in _get_all_machines():
        machine_id = machine.get("id")
        if not machine_id or machine_id == "local":
            continue
        if machine_filter and machine_id != machine_filter:
            continue

        cmd = "tmux list-sessions -F '#{session_name}:#{session_windows}:#{pane_current_path}' 2>/dev/null || echo ''"
        result = _run_remote(machine_id, cmd)
        if result.returncode != 0:
            continue  # unreachable — omit entirely

        machine_sessions = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) < 2:
                continue
            remote_path = parts[2] if len(parts) > 2 else ""
            machine_sessions.append({
                "name": f"{parts[0]}@{machine_id}",
                "windows": int(parts[1]) if parts[1].isdigit() else 1,
                "path": remote_path,
                "machine": machine_id,
                "type": _get_remote_session_type(machine_id, remote_path),
            })
        remote_by_machine[machine_id] = machine_sessions
    return remote_by_machine


def cmd_list(args) -> int:
    """List tmux sessions or panes.

    When inside a tmux session, shows panes by default.
    Use --sessions to show sessions instead.
    """
    json_mode = getattr(args, 'json', False)

    if not _check_tmux_installed():
        return 1 if not json_mode else _output_result(False, json_mode, "tmux is required but not installed")
    local_only = getattr(args, 'local', False)
    remote_only = getattr(args, 'remote', False)
    machine_filter = getattr(args, 'machine', None)
    show_context = getattr(args, 'context', False)
    show_sessions = getattr(args, 'sessions', False) or show_context

    # Check if we're inside a tmux session
    current_session = pane_manager.get_current_session()

    # If inside tmux and not explicitly asking for sessions, show panes
    if current_session and not show_sessions:
        panes = pane_manager.list_panes(current_session)

        if json_mode:
            pane_data = [
                {
                    "index": p.index,
                    "pane_id": p.pane_id,
                    "pid": p.pid,
                    "command": p.command,
                    "active": p.active,
                }
                for p in panes
            ]
            _output_json({"success": True, "session": current_session, "panes": pane_data})
            return 0

        if not panes:
            print(f"No panes in session '{current_session}'")
            return 0

        print(f"Panes in {current_session}:")
        for p in panes:
            active_marker = " *" if p.active else ""
            role = "orchestrator" if p.index == 0 else "worker"
            print(f"  {p.index}: [{role}] {p.command}{active_marker}")
        return 0

    # Show sessions (original behavior)
    local_sessions = [] if remote_only else list_local_sessions(show_context=show_context)
    remote_by_machine = {} if local_only else list_remote_sessions(machine_filter)

    all_sessions = list(local_sessions)
    for machine_sessions in remote_by_machine.values():
        all_sessions.extend(machine_sessions)

    # Output
    if json_mode:
        _output_json({"success": True, "sessions": all_sessions})
        return 0

    # Text output - grouped by machine
    # Combine local and remote sessions into single machine-based view
    all_machines = {}

    # Add local sessions to machine view
    for s in local_sessions:
        machine = s['machine']
        if machine not in all_machines:
            all_machines[machine] = []
        all_machines[machine].append(s)

    # Add remote sessions to machine view
    for machine_id, sessions in remote_by_machine.items():
        if machine_id not in all_machines:
            all_machines[machine_id] = []
        all_machines[machine_id].extend(sessions)

    if not all_machines:
        print("No sessions running")
        return 0

    # Display all sessions grouped by machine
    for machine_id, sessions in sorted(all_machines.items(), key=lambda x: (x[0] is not None, x[0])):
        label = machine_id if machine_id else "local"
        print(f"{label}:")
        if sessions:
            for s in sessions:
                # Remove @machine suffix for display within machine group
                display_name = s['name'].rsplit('@', 1)[0] if '@' in s['name'] else s['name']
                parked_marker = " [parked: usage limit]" if s.get("usage_limit") else ""
                ctx_marker = ""
                if show_context:
                    ctx = s.get("context")
                    if ctx is None:
                        ctx_marker = "  ctx=remote(n/a)"
                    elif ctx.get("remaining_pct") is None:
                        ctx_marker = "  ctx=— (daemon/non-agent)" if not ctx.get("is_agent") else "  ctx=? (no bar)"
                    else:
                        flag = " ⚠ LOW" if ctx.get("flagged") else ""
                        ctx_marker = f"  ctx={ctx['remaining_pct']}% left{flag}"
                print(f"  {display_name}: {s['windows']} window(s) ({s['path']}){parked_marker}{ctx_marker}")
        else:
            print("  (no sessions)")
        print()

    return 0


def cmd_output(args) -> int:
    """Read output from a tmux session or pane.

    Supports remote sessions with session@machine format.
    Use --pane N to read from a specific pane in the current session.
    """
    session_full = getattr(args, 'session', None)
    pane_index = getattr(args, 'pane', None)
    lines = args.lines or 50
    json_mode = getattr(args, 'json', False)

    # Handle pane mode (auto-detect session from environment)
    if pane_index is not None:
        try:
            output = pane_manager.capture_pane(session_full, pane_index, lines)
            if json_mode:
                _output_json({
                    "success": True,
                    "pane": pane_index,
                    "session": session_full or pane_manager.get_current_session(),
                    "lines": lines,
                    "output": output
                })
            else:
                print(output)
            return 0
        except RuntimeError as e:
            return _output_result(False, json_mode, str(e))

    # Session mode (original behavior)
    if not session_full:
        if json_mode:
            print(json.dumps({"success": False, "error": "Session name required (-s) or pane number (--pane)"}))
        else:
            print("Usage: agentwire output -s <session> [-n lines]", file=sys.stderr)
            print("   or: agentwire output --pane N [-n lines]", file=sys.stderr)
        return 1

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)

    if machine_id:
        # Remote: SSH and run tmux capture-pane
        machine = _get_machine_config(machine_id)
        if machine is None:
            if json_mode:
                print(json.dumps({"success": False, "error": f"Machine '{machine_id}' not found"}))
            else:
                print(f"Machine '{machine_id}' not found in machines.json", file=sys.stderr)
            return 1

        cmd = f"tmux capture-pane -t {shlex.quote(session)} -p -S -{lines}"
        result = _run_remote(machine_id, cmd)

        if result.returncode != 0:
            if json_mode:
                print(json.dumps({"success": False, "error": f"Session '{session}' not found on {machine_id}"}))
            else:
                print(f"Session '{session}' not found on {machine_id}", file=sys.stderr)
            return 1

        if json_mode:
            print(json.dumps({
                "success": True,
                "session": session_full,
                "lines": lines,
                "machine": machine_id,
                "output": result.stdout
            }))
        else:
            print(result.stdout)
        return 0

    # Local: existing logic
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if result.returncode != 0:
        if json_mode:
            print(json.dumps({"success": False, "error": f"Session '{session}' not found"}))
        else:
            print(f"Session '{session}' not found", file=sys.stderr)
        return 1

    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
        capture_output=True,
        text=True
    )

    if json_mode:
        print(json.dumps({
            "success": True,
            "session": session_full,
            "lines": lines,
            "machine": None,
            "output": result.stdout
        }))
    else:
        print(result.stdout)
    return 0


def cmd_info(args) -> int:
    """Get session information as JSON.

    Returns working directory, pane count, and other metadata.
    """
    session_full = args.session
    json_mode = getattr(args, 'json', True)  # Default to JSON

    if not session_full:
        return _output_result(False, json_mode, "Session name required (-s)")

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)

    if machine_id:
        # Remote session
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found")

        # Get session info via SSH
        cmd = f"tmux display-message -t {shlex.quote(session)} -p '#{{pane_current_path}}:#{{window_panes}}' 2>/dev/null"
        result = _run_remote(machine_id, cmd)

        if result.returncode != 0:
            return _output_result(False, json_mode, f"Session '{session}' not found on {machine_id}")

        parts = result.stdout.strip().split(":")
        cwd = parts[0] if parts else ""
        pane_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

        info = {
            "success": True,
            "session": session_full,
            "name": session,
            "machine": machine_id,
            "cwd": cwd,
            "pane_count": pane_count,
            "is_remote": True,
        }
    else:
        # Local session
        if not tmux_session_exists(session):
            return _output_result(False, json_mode, f"Session '{session}' not found")

        # Get working directory
        result = subprocess.run(
            ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}:#{window_panes}"],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return _output_result(False, json_mode, f"Could not get info for '{session}'")

        parts = result.stdout.strip().split(":")
        cwd = parts[0] if parts else ""
        pane_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

        # Get pane details
        panes_result = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_index}:#{pane_current_command}:#{pane_active}"],
            capture_output=True,
            text=True,
        )
        panes = []
        if panes_result.returncode == 0:
            for line in panes_result.stdout.strip().split("\n"):
                if line:
                    pane_parts = line.split(":")
                    if len(pane_parts) >= 3:
                        panes.append({
                            "index": int(pane_parts[0]),
                            "command": pane_parts[1],
                            "active": pane_parts[2] == "1",
                        })

        info = {
            "success": True,
            "session": session,
            "name": session,
            "machine": None,
            "cwd": cwd,
            "pane_count": pane_count,
            "panes": panes,
            "is_remote": False,
        }

    if json_mode:
        print(json.dumps(info))
    else:
        print(f"Session: {info['name']}")
        if info['machine']:
            print(f"Machine: {info['machine']}")
        print(f"CWD: {info['cwd']}")
        print(f"Panes: {info['pane_count']}")

    return 0


# Shell commands that mean "no agent running" in a pane — kill directly,
# nothing to /exit.
_SHELL_COMMANDS = {"zsh", "bash", "fish", "sh", "dash", "tcsh", "ksh", "nu", "login"}


def _pane0_state(session: str) -> tuple[str | None, str | None]:
    """Return (current_command, current_path) for pane 0, or (None, None)."""
    result = subprocess.run(
        ["tmux", "display-message", "-t", f"{session}.0", "-p",
         "#{pane_current_command}\t#{pane_current_path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, None
    parts = result.stdout.strip().split("\t", 1)
    command = parts[0] if parts and parts[0] else None
    path = parts[1] if len(parts) > 1 and parts[1] else None
    return command, path


def _wants_graceful_exit(session_type: str | None, pane_command: str | None) -> bool:
    """Whether a session should get /exit before kill.

    Claude-type sessions (claude-* or no declared type) get a graceful
    /exit. Bare shells don't speak /exit — plain tmux kill. If pane 0 is
    just sitting at a shell, there's no agent to exit.
    """
    if pane_command is None or pane_command in _SHELL_COMMANDS:
        return False
    if session_type == "bare":
        return False
    return True


def cmd_kill(args) -> int:
    """Kill a tmux session or pane (graceful /exit first, then kill).

    Claude sessions get /exit and we wait for the agent to actually
    terminate (up to --timeout) before killing tmux. Bare sessions fall
    through to a plain tmux kill. --force skips the graceful step entirely.

    Supports remote sessions with session@machine format.
    Use --pane N to kill a specific pane in the current session.
    """
    session_full = getattr(args, 'session', None)
    pane_index = getattr(args, 'pane', None)
    json_mode = getattr(args, 'json', False)
    force = getattr(args, 'force', False)
    timeout = getattr(args, 'timeout', 10)

    # Handle pane mode (auto-detect session from environment)
    if pane_index is not None:
        if pane_index == 0:
            return _output_result(False, json_mode, "Cannot kill pane 0 (orchestrator)")

        try:
            session = session_full or pane_manager.get_current_session()
            if not session:
                return _output_result(False, json_mode, "Not in tmux session and no session specified")

            # Send /exit for clean shutdown (use send_to_pane for proper timing)
            pane_manager.send_to_pane(session, pane_index, "/exit")
            if not json_mode:
                print(f"Sent /exit to pane {pane_index}, waiting 3s...")
            time.sleep(3)

            # Kill the pane
            pane_manager.kill_pane(session, pane_index)

            if json_mode:
                _output_json({
                    "success": True,
                    "pane": pane_index,
                    "session": session,
                })
            else:
                print(f"Killed pane {pane_index}")
            return 0
        except RuntimeError as e:
            return _output_result(False, json_mode, str(e))

    # Session mode (original behavior)
    if not session_full:
        return _output_result(False, json_mode, "Usage: agentwire kill -s <session> or --pane N")

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)

    if machine_id:
        # Remote: SSH and run tmux commands
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        # Check if session exists + grab pane 0 state in one round-trip
        q = shlex.quote(session)
        state_cmd = (
            f"tmux display-message -t {q}.0 -p "
            "'#{pane_current_command}\t#{pane_current_path}' 2>/dev/null"
        )
        result = _run_remote(machine_id, state_cmd)
        if result.returncode != 0 or not result.stdout.strip():
            return _output_result(False, json_mode, f"Session '{session}' not found on {machine_id}")

        parts = result.stdout.strip().split("\t", 1)
        pane_command = parts[0] if parts and parts[0] else None
        pane_path = parts[1] if len(parts) > 1 and parts[1] else None
        session_type = _get_remote_session_type(machine_id, pane_path) if pane_path else None

        graceful = not force and _wants_graceful_exit(session_type, pane_command)
        agent_exited = False
        if graceful:
            # Send /exit to Claude first for clean shutdown (target pane 0 specifically)
            _run_remote(machine_id, f"tmux send-keys -t {q}.0 /exit Enter")
            if not json_mode:
                print(f"Sent /exit to {session_full}, waiting for agent to exit (up to {timeout}s)...")
            # Poll over SSH (1s interval — each check is a round-trip)
            poll_cmd = (
                f"tmux display-message -t {q}.0 -p '#{{pane_current_command}}' 2>/dev/null"
                " || echo __GONE__"
            )
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(1)
                poll = _run_remote(machine_id, poll_cmd)
                current = poll.stdout.strip()
                if current == "__GONE__" or not current or current in _SHELL_COMMANDS:
                    agent_exited = True
                    break

        # Kill the session
        _run_remote(machine_id, f"tmux kill-session -t {q} 2>/dev/null")
        if not json_mode:
            print(f"Killed session '{session_full}'")

        _notify_portal_sessions_changed()

        if json_mode:
            _output_json({
                "success": True,
                "session": session_full,
                "graceful": graceful,
                "agent_exited": agent_exited,
            })
        return 0

    # Local
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if result.returncode != 0:
        return _output_result(False, json_mode, f"Session '{session}' not found")

    pane_command, pane_path = _pane0_state(session)
    session_type = _get_session_type_from_path(pane_path) if pane_path else None

    graceful = not force and _wants_graceful_exit(session_type, pane_command)
    agent_exited = False
    if graceful:
        # Send /exit to Claude first for clean shutdown
        # Target pane 0 specifically and capture output to avoid terminal noise
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{session}.0", "/exit", "Enter"],
            capture_output=True
        )
        if not json_mode:
            print(f"Sent /exit to {session}, waiting for agent to exit (up to {timeout}s)...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode != 0:
                # Agent exit took the whole session down
                agent_exited = True
                break
            current, _ = _pane0_state(session)
            if current is None or current in _SHELL_COMMANDS:
                agent_exited = True
                break

    # Kill the session (no-op if the agent exit already tore it down)
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    if not json_mode:
        print(f"Killed session '{session}'")

    # Drop session metadata (creator record) so a future unrelated session
    # reusing this name doesn't inherit a stale parent.
    metadata_file = CONFIG_DIR / "sessions" / session.split("@")[0] / "metadata.json"
    if metadata_file.exists():
        try:
            metadata_file.unlink()
        except OSError:
            pass

    # GC this sender's still-pending outbound across every recipient inbox (#621)
    # so report-backs it left undelivered don't accumulate. Load-bearing kinds
    # dead-letter (and escalate via owner email); the rest are dropped.
    try:
        from . import inbox
        inbox.gc_sender(session)
    except Exception:
        pass

    _notify_portal_sessions_changed()

    if json_mode:
        _output_json({
            "success": True,
            "session": session_full,
            "graceful": graceful,
            "agent_exited": agent_exited,
        })
    return 0


def _wait_for_worker_ready(session: str, pane_index: int, timeout: int = 30, agent_type: str = "claude") -> bool:
    """Wait for a worker pane to be ready to receive input.

    Pane-scoped with loose indicators ('>', 'Claude Code') to support
    non-claude worker types — deliberately NOT consolidated into
    session_ready.wait_for_session_ready, which is claude-banner specific.

    Polls the pane output looking for Claude Code's '❯' prompt.

    Returns True if worker became ready, False if timeout.
    """
    import time

    start = time.time()
    poll_interval = 0.5  # Check every 500ms

    ready_indicators = ['❯', '>', 'Claude Code']

    while (time.time() - start) < timeout:
        try:
            # Use session.N to target pane N (omit window index, let tmux resolve it)
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", f"{session}.{pane_index}", "-p", "-S", "-20"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                output = result.stdout
                # Check for any ready indicator
                for indicator in ready_indicators:
                    if indicator in output:
                        # Extra wait to ensure fully ready
                        time.sleep(0.3)
                        return True
        except Exception:
            pass

        time.sleep(poll_interval)

    return False


def cmd_spawn(args) -> int:
    """Spawn a worker pane in the current session.

    Creates a new tmux pane in the orchestrator's session and starts
    Claude Code with the specified roles (default: worker).

    With --branch, creates an isolated worktree for the worker to enable
    parallel commits without conflicts.

    By default, waits for the worker to be ready before returning.
    Use --no-wait to return immediately after spawning.
    """
    json_mode = getattr(args, 'json', False)
    cwd = getattr(args, 'cwd', None)
    roles_arg = getattr(args, 'roles', None)
    session = getattr(args, 'session', None)
    branch = getattr(args, 'branch', None)
    no_wait = getattr(args, 'no_wait', False)
    timeout = getattr(args, 'timeout', 30)

    # If cwd not specified, use the target session's pane 0 directory
    if not cwd:
        target_session = session or pane_manager.get_current_session()
        if target_session:
            result = subprocess.run(
                ["tmux", "display", "-t", f"{target_session}.0", "-p", "#{pane_current_path}"],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                cwd = result.stdout.strip()
        if not cwd:
            cwd = os.getcwd()

    worktree_path = None

    # Handle --branch: create worktree for isolated work
    if branch:
        try:
            worktree_path = pane_manager.create_worker_worktree(branch, cwd)
            cwd = worktree_path
            if not json_mode:
                print(f"Created worktree at {worktree_path}")
        except RuntimeError as e:
            return _output_result(False, json_mode, f"Failed to create worktree: {e}")

    # Resolve roles via the shared resolver: kind="worker" is a SAFETY-RAIL
    # kind — the worker etiquette (focus / report / auto-kill) is always
    # present and non-overridable; --roles and .agentwire.yml roles: STACK on
    # top of it. soul is a no-op for workers (headless) but kept for parity.
    cli_roles = [r.strip() for r in roles_arg.split(",") if r.strip()] if roles_arg else None
    project_cfg = load_project_config(Path(cwd))
    project_roles = project_cfg.roles if (project_cfg and project_cfg.roles) else None
    role_names = resolve_roles("worker", cli_roles=cli_roles, project_roles=project_roles)
    role_names = inject_soul(role_names, load_config(), no_soul=getattr(args, 'no_soul', False))

    # Load and validate roles
    roles, missing = load_roles(role_names, Path(cwd))
    if missing:
        return _output_result(False, json_mode, f"Roles not found: {', '.join(missing)}")

    # Resolve session type via the shared posture core (kind=worker
    # defaults to the restricted posture).
    session_type_str, st_err = _resolve_session_type_from_args(args, "worker")
    if st_err:
        return _output_result(False, json_mode, st_err)

    # Build agent command
    agent = build_agent_command(session_type_str, roles if roles else None, model=getattr(args, 'model', None))
    agent.env.update(parse_env_args(getattr(args, 'env', None)))

    agent_cmd = agent.command

    try:
        # Inject secrets onto the parent session before spawning the pane so the
        # new pane inherits them from tmux (avoids putting keys in `ps`).
        parent_session = session or pane_manager.get_current_session()
        if parent_session:
            inject_session_env(parent_session, agent.env)

        # Spawn pane first to get the pane index
        pane_index = pane_manager.spawn_worker_pane(
            session=session,
            cwd=cwd,
            cmd=agent_cmd
        )

        # Install pane hook to notify portal when pane exits
        actual_session = session or pane_manager.get_current_session()
        _install_pane_hooks(actual_session, pane_index)

        # Wait for worker to be ready (unless --no-wait)
        worker_ready = True
        if not no_wait:
            worker_ready = _wait_for_worker_ready(actual_session, pane_index, timeout)

        if json_mode:
            result = {
                "success": True,
                "pane": pane_index,
                "session": actual_session,
                "roles": role_names,
                "ready": worker_ready,
            }
            if branch:
                result["branch"] = branch
                result["worktree"] = worktree_path
            _output_json(result)
        else:
            if worker_ready:
                print(f"Spawned pane {pane_index}")
            else:
                print(f"Spawned pane {pane_index} (timeout waiting for ready)")

        return 0

    except RuntimeError as e:
        return _output_result(False, json_mode, str(e))


def cmd_split(args) -> int:
    """Add terminal pane(s) to current session with even vertical layout."""
    count = getattr(args, 'count', 1)
    cwd = getattr(args, 'cwd', None) or os.getcwd()
    session = getattr(args, 'session', None)

    # Get current session if not specified
    if not session:
        session = os.environ.get("TMUX_PANE")
        if session:
            # We're in tmux, get session name
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#{session_name}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                session = result.stdout.strip()
            else:
                session = None

    if not session:
        print("Error: Not in a tmux session and no --session specified")
        return 1

    # Verify session exists
    check = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if check.returncode != 0:
        print(f"Error: Session '{session}' not found")
        return 1

    # Add panes
    for i in range(count):
        result = subprocess.run([
            "tmux", "split-window", "-v", "-t", session, "-c", cwd
        ], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error: Failed to split pane: {result.stderr.strip()}")
            return 1

    # Apply main-top layout: orchestrator (pane 0) at top with 60%, workers below
    pane_manager._apply_main_top_layout(session)
    subprocess.run(["tmux", "select-pane", "-t", f"{session}.0"], capture_output=True)

    pane_count = 1 + count  # original + new
    print(f"Added {count} pane(s) - now {pane_count} panes")
    return 0


def cmd_detach(args) -> int:
    """Move a pane to its own session and re-align remaining panes."""
    pane_index = getattr(args, 'pane', None)
    new_session = getattr(args, 'session', None)
    source_session = getattr(args, 'source', None)

    if pane_index is None or new_session is None:
        print("Error: --pane and -s/--session are required")
        return 1

    # Get source session if not specified
    if not source_session:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            source_session = result.stdout.strip()
        else:
            print("Error: Could not detect current session")
            return 1

    # Check if target session already exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", new_session],
        capture_output=True
    )
    session_exists = result.returncode == 0

    # Verify source session exists
    check = subprocess.run(
        ["tmux", "has-session", "-t", source_session],
        capture_output=True
    )
    if check.returncode != 0:
        print(f"Error: Session '{source_session}' not found")
        return 1

    # Move pane to new session
    if session_exists:
        # Move to existing session
        result = subprocess.run([
            "tmux", "move-pane", "-s", f"{source_session}:{pane_index}", "-t", f"{new_session}:"
        ], capture_output=True, text=True)
    else:
        # Break pane into new session
        result = subprocess.run([
            "tmux", "break-pane", "-d", "-s", f"{source_session}:{pane_index}", "-t", f"{new_session}:"
        ], capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error: Failed to detach pane {pane_index}: {result.stderr.strip()}")
        return 1

    # Re-align remaining panes with main-top layout
    pane_manager._apply_main_top_layout(source_session)
    subprocess.run(["tmux", "select-pane", "-t", f"{source_session}.0"], capture_output=True)

    print(f"Moved pane {pane_index} to session '{new_session}'")
    return 0


def cmd_jump(args) -> int:
    """Jump to (focus) a specific pane."""
    json_mode = getattr(args, 'json', False)
    pane_index = getattr(args, 'pane', None)
    session = getattr(args, 'session', None)

    if pane_index is None:
        return _output_result(False, json_mode, "Usage: agentwire jump --pane N")

    try:
        pane_manager.focus_pane(session, pane_index)

        if json_mode:
            _output_json({
                "success": True,
                "pane": pane_index,
                "session": session or pane_manager.get_current_session(),
            })
        else:
            print(f"Jumped to pane {pane_index}")

        return 0

    except RuntimeError as e:
        return _output_result(False, json_mode, str(e))


def cmd_resize(args) -> int:
    """Re-fit tmux window to its attached clients per the window-size policy."""
    json_mode = getattr(args, 'json', False)
    session = getattr(args, 'session', None)

    # Get session name
    if not session:
        session = pane_manager.get_current_session()
        if not session:
            return _output_result(False, json_mode, "Not in a tmux session. Use -s to specify session.")

    try:
        # Unset any window-level window-size override (e.g. manual mode left
        # by resize-window -x/-y) so the configured policy re-fits the window.
        # resize-window -A would resize once but leave manual mode set (#258).
        result = subprocess.run(
            ["tmux", "set-option", "-w", "-t", session, "-u", "window-size"],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to resize: {result.stderr.strip()}")

        if json_mode:
            _output_json({"success": True, "session": session})
        else:
            print(f"Re-fit {session} to attached clients per window-size policy")

        return 0

    except Exception as e:
        return _output_result(False, json_mode, str(e))


def register_pane_parser(subparsers) -> None:
    # === list command (top-level) ===
    list_parser = subparsers.add_parser("list", help="List panes (in tmux) or sessions")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")
    list_parser.add_argument("--local", action="store_true", help="Only show local sessions")
    list_parser.add_argument("--remote", action="store_true", help="Only show remote sessions")
    list_parser.add_argument("--machine", help="Filter by specific machine ID")
    list_parser.add_argument("--sessions", action="store_true", help="Show sessions instead of panes")
    list_parser.add_argument("--context", action="store_true",
                             help="Annotate each session with its Claude Code context headroom "
                                  "(remaining %%); flags sessions running low (implies --sessions)")
    list_parser.set_defaults(func=cmd_list)

    # === output command (top-level) ===
    output_parser = subparsers.add_parser("output", help="Read session or pane output")
    output_parser.add_argument("-s", "--session", help="Session name (supports session@machine)")
    output_parser.add_argument("--pane", type=int, help="Target pane index (auto-detects session)")
    output_parser.add_argument("-n", "--lines", type=int, default=50, help="Lines to show (default: 50)")
    output_parser.add_argument("--json", action="store_true", help="Output as JSON")
    output_parser.set_defaults(func=cmd_output)

    # === info command (top-level) ===
    info_parser = subparsers.add_parser("info", help="Get session information (cwd, panes, etc.)")
    info_parser.add_argument("-s", "--session", required=True, help="Session name (supports session@machine)")
    info_parser.add_argument("--json", action="store_true", default=True, help="Output as JSON (default)")
    info_parser.add_argument("--no-json", dest="json", action="store_false", help="Human-readable output")
    info_parser.set_defaults(func=cmd_info)

    # === kill command (top-level) ===
    kill_parser = subparsers.add_parser("kill", help="Kill a session or pane (graceful /exit, then tmux kill)")
    kill_parser.add_argument("-s", "--session", help="Session name (supports session@machine)")
    kill_parser.add_argument("--pane", type=int, help="Target pane index (auto-detects session)")
    kill_parser.add_argument("--force", action="store_true", help="Skip graceful /exit, kill tmux session immediately")
    kill_parser.add_argument("--timeout", type=int, default=10, help="Seconds to wait for agent to exit after /exit (default: 10)")
    kill_parser.add_argument("--json", action="store_true", help="Output as JSON")
    kill_parser.set_defaults(func=cmd_kill)

    # === spawn command (top-level) ===
    spawn_parser = subparsers.add_parser("spawn", help="Spawn a worker pane in current session")
    spawn_parser.add_argument("-s", "--session", help="Target session (default: auto-detect)")
    spawn_parser.add_argument("--cwd", help="Working directory (default: current)")
    spawn_parser.add_argument("--branch", "-b", help="Create worktree on this branch for isolated commits")
    _add_posture_flag(spawn_parser)
    spawn_parser.add_argument("--type", help="Legacy fused session type (accepted, not primary): claude-bypass, claude-restricted")
    spawn_parser.add_argument("--roles", default=None, help="Comma-separated roles, STACKED on top of the always-present worker etiquette")
    spawn_parser.add_argument("--model", help="Model override (e.g., haiku, sonnet, opus)")
    spawn_parser.add_argument("--no-soul", dest="no_soul", action="store_true", help="Skip soul personality role injection (no-op for the headless worker role)")
    spawn_parser.add_argument("--no-wait", action="store_true", help="Don't wait for worker to be ready (default: wait up to 30s)")
    spawn_parser.add_argument("--timeout", type=int, default=30, help="Seconds to wait for worker ready (default: 30)")
    spawn_parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var onto parent session (repeatable)")
    spawn_parser.add_argument("--json", action="store_true", help="Output as JSON")
    spawn_parser.set_defaults(func=cmd_spawn)

    # === split command (top-level) ===
    split_parser = subparsers.add_parser("split", help="Add terminal pane(s) with even vertical layout")
    split_parser.add_argument("-n", "--count", type=int, default=1, help="Number of panes to add (default: 1)")
    split_parser.add_argument("-s", "--session", help="Target session (default: auto-detect)")
    split_parser.add_argument("--cwd", help="Working directory (default: current)")
    split_parser.set_defaults(func=cmd_split)

    # === detach command (top-level) ===
    detach_parser = subparsers.add_parser("detach", help="Move a pane to its own session")
    detach_parser.add_argument("--pane", type=int, required=True, help="Pane index to detach")
    detach_parser.add_argument("-s", "--session", required=True, help="Target session name (created if doesn't exist)")
    detach_parser.add_argument("--source", help="Source session (default: auto-detect)")
    detach_parser.set_defaults(func=cmd_detach)

    # === jump command (top-level) ===
    jump_parser = subparsers.add_parser("jump", help="Jump to (focus) a specific pane")
    jump_parser.add_argument("-s", "--session", help="Target session (default: auto-detect)")
    jump_parser.add_argument("--pane", type=int, required=True, help="Pane index to focus")
    jump_parser.add_argument("--json", action="store_true", help="Output as JSON")
    jump_parser.set_defaults(func=cmd_jump)

    # === resize command (top-level) ===
    resize_parser = subparsers.add_parser("resize", help="Re-fit window to attached clients per window-size policy")
    resize_parser.add_argument("-s", "--session", help="Target session (default: auto-detect)")
    resize_parser.add_argument("--json", action="store_true", help="Output as JSON")
    resize_parser.set_defaults(func=cmd_resize)
