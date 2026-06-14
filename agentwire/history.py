"""
Claude Code conversation history utilities.

Reads conversation data from:
- ~/.claude/history.jsonl - user message history with timestamps and projects
- ~/.claude/projects/{encoded-path}/*.jsonl - session files with summaries

Supports both local and remote machines via SSH for distributed setups.
"""

import json
from pathlib import Path

from .ssh import ssh_base_opts
from .utils.file_io import load_json
from .utils.paths import agentwire_dir
from .utils.subprocess import run_command

# Claude Code data directories
CLAUDE_DIR = Path.home() / ".claude"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def resolve_session_id(prefix: str, machine: str = "local") -> str | None:
    """Resolve a session ID prefix to full UUID.

    Args:
        prefix: Session ID prefix (e.g., "b52e2fac" or full UUID)
        machine: Machine ID or 'local'

    Returns:
        Full session ID if unique match found, None otherwise.
    """
    # If it looks like a full UUID, return as-is
    if len(prefix) == 36 and prefix.count("-") == 4:
        return prefix

    # Search history for matching session IDs
    if machine == "local":
        history_path = str(HISTORY_FILE)
    else:
        history_path = "~/.claude/history.jsonl"

    content = _read_file_content(history_path, machine)
    if not content:
        return None

    matches = set()
    for line in content.strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
            session_id = entry.get("sessionId", "")
            if session_id.startswith(prefix):
                matches.add(session_id)
        except json.JSONDecodeError:
            continue

    # Return if exactly one match
    if len(matches) == 1:
        return matches.pop()

    return None


def encode_project_path(path: str) -> str:
    """Encode project path to Claude's directory format.

    Example: /home/user/projects/myapp -> -home-user-projects-myapp
    """
    # Replace all slashes with dashes (leading slash becomes leading dash)
    return path.replace("/", "-")


def decode_project_path(encoded: str) -> str:
    """Decode Claude's directory format back to path.

    Example: -home-user-projects-myapp -> /home/user/projects/myapp
    """
    # Replace dashes with slashes (leading dash becomes leading slash)
    return encoded.replace("-", "/")


def _get_machine_config(machine_id: str) -> dict | None:
    """Load machine config from machines.json.

    Args:
        machine_id: Machine identifier.

    Returns:
        Machine config dict or None if not found.
    """
    machines_file = agentwire_dir() / "machines.json"
    machines_data = load_json(machines_file, default={})

    machines = machines_data.get("machines", [])
    for m in machines:
        if m.get("id") == machine_id:
            return m

    return None


def _run_ssh_command(machine: dict, command: str, timeout: int = 10) -> tuple[bool, str]:
    """Run command on remote machine via SSH.

    Args:
        machine: Machine config dict with host, user, port
        command: Shell command to run
        timeout: Command timeout in seconds

    Returns:
        (success, output) tuple
    """
    host = machine.get("host", machine.get("id", ""))
    user = machine.get("user")
    port = machine.get("port")

    # Build SSH target
    if user:
        ssh_target = f"{user}@{host}"
    else:
        ssh_target = host

    # Build SSH command with connection timeout
    ssh_cmd = ["ssh", *ssh_base_opts(), "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
    if port:
        ssh_cmd.extend(["-p", str(port)])
    ssh_cmd.extend([ssh_target, command])

    result = run_command(ssh_cmd, timeout=timeout)
    return result.success, result.stdout


def _read_file_content(filepath: str, machine: str = "local") -> str | None:
    """Read file content, local or remote.

    Args:
        filepath: Path to file
        machine: Machine ID or 'local'

    Returns:
        File content as string, or None if not found/error
    """
    if machine == "local":
        path = Path(filepath)
        if not path.exists():
            return None
        try:
            return path.read_text()
        except IOError:
            return None
    else:
        machine_config = _get_machine_config(machine)
        if not machine_config:
            return None
        success, output = _run_ssh_command(machine_config, f"cat '{filepath}' 2>/dev/null")
        return output if success else None


def _list_directory(dirpath: str, machine: str = "local") -> list[str]:
    """List files in directory, local or remote.

    Args:
        dirpath: Path to directory
        machine: Machine ID or 'local'

    Returns:
        List of filenames (not full paths)
    """
    if machine == "local":
        path = Path(dirpath)
        if not path.exists() or not path.is_dir():
            return []
        return [f.name for f in path.iterdir() if f.is_file()]
    else:
        machine_config = _get_machine_config(machine)
        if not machine_config:
            return []
        success, output = _run_ssh_command(
            machine_config, f"ls -1 '{dirpath}' 2>/dev/null", timeout=15
        )
        if not success:
            return []
        return [f for f in output.strip().split("\n") if f]


def _grep_file(filepath: str, pattern: str, machine: str = "local") -> list[str]:
    """Grep lines matching pattern from file.

    Args:
        filepath: Path to file
        pattern: grep pattern to match
        machine: Machine ID or 'local'

    Returns:
        List of matching lines
    """
    if machine == "local":
        path = Path(filepath)
        if not path.exists():
            return []
        result = run_command(["grep", "-E", pattern, str(path)], timeout=5)
        return [line for line in result.stdout.strip().split("\n") if line]
    else:
        machine_config = _get_machine_config(machine)
        if not machine_config:
            return []
        success, output = _run_ssh_command(
            machine_config, f"grep -E '{pattern}' '{filepath}' 2>/dev/null", timeout=10
        )
        if not success:
            return []
        return [line for line in output.strip().split("\n") if line]


def get_history(project_path: str, machine: str = "local", limit: int = 20) -> list[dict]:
    """Get conversation history for a project.

    Reads from ~/.claude/history.jsonl and enriches with summaries from session files.

    Args:
        project_path: Absolute path to project directory
        machine: Machine ID or 'local'
        limit: Maximum number of sessions to return

    Returns:
        List of session dicts: {sessionId, firstMessage, lastSummary, timestamp, messageCount}
        Sorted by timestamp descending (newest first).
    """
    # Determine paths based on machine
    if machine == "local":
        history_path = str(HISTORY_FILE)
        projects_base = str(PROJECTS_DIR)
    else:
        history_path = "~/.claude/history.jsonl"
        projects_base = "~/.claude/projects"

    # Read history.jsonl
    content = _read_file_content(history_path, machine)
    if not content:
        return []

    # Parse and filter by project
    sessions: dict[str, dict] = {}  # sessionId -> session data

    for line in content.strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Filter by project path
        if entry.get("project") != project_path:
            continue

        session_id = entry.get("sessionId")
        if not session_id:
            continue

        timestamp = entry.get("timestamp", 0)
        display = entry.get("display", "")

        if session_id not in sessions:
            sessions[session_id] = {
                "sessionId": session_id,
                "firstMessage": display,
                "lastSummary": None,
                "timestamp": timestamp,
                "messageCount": 1,
            }
        else:
            sessions[session_id]["messageCount"] += 1
            # Update timestamp if newer
            if timestamp > sessions[session_id]["timestamp"]:
                sessions[session_id]["timestamp"] = timestamp

    # Get summaries from session files
    encoded_path = encode_project_path(project_path)
    session_dir = f"{projects_base}/{encoded_path}"

    for session_id in sessions:
        session_file = f"{session_dir}/{session_id}.jsonl"
        # Grep for summary lines only - more efficient than full parse
        summary_lines = _grep_file(session_file, '"type":"summary"', machine)
        if summary_lines:
            # Get the last summary
            try:
                last_summary = json.loads(summary_lines[-1])
                sessions[session_id]["lastSummary"] = last_summary.get("summary")
            except json.JSONDecodeError:
                pass

    # Sort by timestamp descending and limit
    result = sorted(sessions.values(), key=lambda x: x["timestamp"], reverse=True)
    return result[:limit]


def get_session_detail(session_id: str, machine: str = "local") -> dict | None:
    """Get full details for a specific session.

    Args:
        session_id: UUID of the session (or unique prefix)
        machine: Machine ID or 'local'

    Returns:
        Session dict: {sessionId, summaries, firstMessage, timestamps: {start, end}, gitBranch, messageCount}
        None if session not found.
    """
    # Resolve prefix to full session ID
    resolved = resolve_session_id(session_id, machine)
    if resolved:
        session_id = resolved

    # First find the session in history to get project path
    if machine == "local":
        history_path = str(HISTORY_FILE)
        projects_base = str(PROJECTS_DIR)
    else:
        history_path = "~/.claude/history.jsonl"
        projects_base = "~/.claude/projects"

    content = _read_file_content(history_path, machine)
    if not content:
        return None

    # Find messages for this session
    messages = []
    project_path = None

    for line in content.strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("sessionId") == session_id:
            messages.append(entry)
            if not project_path:
                project_path = entry.get("project")

    if not messages or not project_path:
        return None

    # Sort messages by timestamp
    messages.sort(key=lambda x: x.get("timestamp", 0))

    # Get session file for summaries and git branch
    encoded_path = encode_project_path(project_path)
    session_file = f"{projects_base}/{encoded_path}/{session_id}.jsonl"

    summaries = []
    git_branch = None

    # Grep for summary lines
    summary_lines = _grep_file(session_file, '"type":"summary"', machine)
    for line in summary_lines:
        try:
            entry = json.loads(line)
            if entry.get("summary"):
                summaries.append(entry.get("summary"))
        except json.JSONDecodeError:
            pass

    # Try to get git branch from first file-history-snapshot entry
    # This is a simple grep - we just look for gitBranch in any line
    branch_lines = _grep_file(session_file, '"gitBranch"', machine)
    if branch_lines:
        try:
            # Parse the first entry that has gitBranch
            for line in branch_lines:
                entry = json.loads(line)
                if "gitBranch" in str(entry):
                    # Could be nested in various places
                    if isinstance(entry.get("gitBranch"), str):
                        git_branch = entry["gitBranch"]
                        break
        except json.JSONDecodeError:
            pass

    return {
        "sessionId": session_id,
        "summaries": summaries,
        "firstMessage": messages[0].get("display", "") if messages else None,
        "timestamps": {
            "start": messages[0].get("timestamp") if messages else None,
            "end": messages[-1].get("timestamp") if messages else None,
        },
        "gitBranch": git_branch,
        "messageCount": len(messages),
    }


def get_project_sessions(project_path: str, machine: str = "local") -> list[str]:
    """Get list of session IDs for a project.

    This reads the session files directory rather than history.jsonl,
    which may include sessions without user messages.

    Args:
        project_path: Absolute path to project directory
        machine: Machine ID or 'local'

    Returns:
        List of session ID strings
    """
    if machine == "local":
        projects_base = str(PROJECTS_DIR)
    else:
        projects_base = "~/.claude/projects"

    encoded_path = encode_project_path(project_path)
    session_dir = f"{projects_base}/{encoded_path}"

    files = _list_directory(session_dir, machine)
    session_ids = []
    for f in files:
        if f.endswith(".jsonl"):
            session_ids.append(f[:-6])  # Remove .jsonl extension

    return session_ids
