"""AgentWire MCP Server.

Exposes AgentWire capabilities as MCP tools for external agents.
This allows tools like MoltBot, Claude Desktop, etc. to manage
tmux sessions, remote machines, and voice features.

Usage:
    agentwire mcp  # Starts MCP server on stdio
"""

import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Configure logging to stderr (stdout is reserved for MCP JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("agentwire-mcp")

# Initialize FastMCP server
mcp = FastMCP(
    name="agentwire",
    instructions="AgentWire MCP server for terminal session management, remote machines, and voice interface for AI agents.",
)


# =============================================================================
# Configuration
# =============================================================================


def get_portal_url() -> str:
    """Get portal URL from environment or config.

    Resolution order:
    1. AGENTWIRE_PORTAL_URL env var
    2. ~/.agentwire/config.yaml → portal.url
    3. Default: https://localhost:8765
    """
    # 1. Environment variable
    if url := os.environ.get("AGENTWIRE_PORTAL_URL"):
        return url

    # 2. Config file
    config_path = Path.home() / ".agentwire" / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
                if url := config.get("portal", {}).get("url"):
                    return url
        except Exception as e:
            logger.warning(f"Failed to read config: {e}")

    # 3. Default
    return "https://localhost:8765"


# =============================================================================
# Caller identity
# =============================================================================


def get_caller_session() -> str | None:
    """Get the tmux session name of the calling agent.

    The MCP server runs inside the caller's tmux session,
    so we can detect their session name from $TMUX_PANE.
    """
    tmux_pane = os.environ.get("TMUX_PANE")
    if not tmux_pane:
        return None
    try:
        result = subprocess.run(
            ["tmux", "display", "-t", tmux_pane, "-p", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# =============================================================================
# CLI Helpers
# =============================================================================


def run_agentwire_cmd(
    args: list[str],
    json_output: bool = True,
    timeout: int = 30,
) -> dict:
    """Run agentwire CLI command and return result.

    Args:
        args: Command arguments (e.g., ["list", "--sessions"])
        json_output: Whether to add --json flag and parse output
        timeout: Command timeout in seconds (default: 30)

    Returns:
        Dict with 'success', 'output', and possibly other fields from JSON output.
        For JSON responses without 'success' field, wraps data with success=True.
    """
    cmd = ["agentwire"] + args
    if json_output:
        cmd.append("--json")

    logger.debug(f"Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        # Try to parse JSON output
        if json_output and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                # Handle JSON arrays (e.g., history list returns [...])
                if isinstance(data, list):
                    return {
                        "success": result.returncode == 0,
                        "items": data,
                    }
                # If the response is valid JSON but doesn't have 'success',
                # wrap it with success based on return code
                if "success" not in data:
                    return {
                        "success": result.returncode == 0,
                        **data,
                    }
                return data
            except json.JSONDecodeError:
                pass

        # Fall back to raw output
        return {
            "success": result.returncode == 0,
            "output": result.stdout.strip(),
            "error": result.stderr.strip() if result.returncode != 0 else None,
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Command timed out"}
    except FileNotFoundError:
        return {"success": False, "error": "agentwire command not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def format_sessions(data: dict) -> str:
    """Format sessions list for LLM consumption."""
    sessions = data.get("sessions", [])
    if not sessions:
        return "No active sessions."

    lines = ["Active sessions:"]
    for s in sessions:
        machine = s.get("machine") or "local"
        name = s.get("name", "unknown")
        windows = s.get("windows", 1)
        path = s.get("path", "")
        session_type = s.get("type", "unknown")
        lines.append(f"  - {name} ({machine}): {windows} window(s), type={session_type}, path={path}")

    return "\n".join(lines)


def format_panes(data: dict) -> str:
    """Format panes list for LLM consumption."""
    panes = data.get("panes", [])
    session = data.get("session", "unknown")

    if not panes:
        return f"No panes in session '{session}'."

    lines = [f"Panes in session '{session}':"]
    for p in panes:
        idx = p.get("index", 0)
        cmd = p.get("command", "unknown")
        active = " (active)" if p.get("active") else ""
        role = "orchestrator" if idx == 0 else "worker"
        lines.append(f"  - Pane {idx} [{role}]: {cmd}{active}")

    return "\n".join(lines)


def format_machines(data: dict) -> str:
    """Format machines list for LLM consumption."""
    machines = data.get("machines", [])
    if not machines:
        return "No remote machines configured."

    lines = ["Configured machines:"]
    for m in machines:
        mid = m.get("id", "unknown")
        host = m.get("host", "unknown")
        user = m.get("user", "")
        status = m.get("status", "unknown")
        user_str = f"{user}@" if user else ""
        lines.append(f"  - {mid}: {user_str}{host} (status: {status})")

    return "\n".join(lines)


def format_projects(data: dict) -> str:
    """Format projects list for LLM consumption."""
    projects = data.get("projects", [])
    if not projects:
        return "No projects found."

    lines = ["Available projects:"]
    for p in projects:
        name = p.get("name", "unknown")
        path = p.get("path", "")
        has_config = p.get("has_config", False)
        config_marker = " (has .agentwire.yml)" if has_config else ""
        lines.append(f"  - {name}: {path}{config_marker}")

    return "\n".join(lines)


def format_roles(data: dict) -> str:
    """Format roles list for LLM consumption."""
    roles = data.get("roles", [])
    if not roles:
        return "No roles available."

    lines = ["Available roles:"]
    for r in roles:
        name = r.get("name", "unknown")
        desc = r.get("description", "")
        source = r.get("source", "")
        lines.append(f"  - {name}: {desc} ({source})")

    return "\n".join(lines)


def format_voices(data: dict) -> str:
    """Format voices list for LLM consumption."""
    voices = data.get("voices", [])
    if not voices:
        return "No custom voices available. Default voice will be used."

    lines = ["Available voices:"]
    for v in voices:
        name = v.get("name", "unknown") if isinstance(v, dict) else v
        lines.append(f"  - {name}")

    return "\n".join(lines)


# =============================================================================
# Session Management Tools
# =============================================================================


@mcp.tool()
def sessions_list() -> str:
    """List all active AgentWire sessions.

    Returns information about all tmux sessions including name, machine,
    window count, working directory, and session type.

    Returns:
        Formatted list of sessions or error message.
    """
    data = run_agentwire_cmd(["list", "--sessions"])
    if not data.get("success"):
        return f"Failed to list sessions: {data.get('error', 'Unknown error')}"
    return format_sessions(data)


@mcp.tool()
def session_create(
    name: str,
    project_dir: str | None = None,
    roles: str | None = None,
    session_type: str | None = None,
) -> str:
    """Create a new AgentWire session.

    Args:
        name: Session name (required)
        project_dir: Project directory path (optional)
        roles: Comma-separated list of roles to apply (optional)
        session_type: Session type like 'claude-bypass', 'claudeglm-bypass' (optional)

    Returns:
        Success message or error description.
    """
    args = ["new", "-s", name]

    if project_dir:
        args.extend(["-p", project_dir])
    if roles:
        args.extend(["--roles", roles])
    if session_type:
        args.extend(["--type", session_type])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Session '{name}' created successfully."
    return f"Failed to create session: {data.get('error', 'Unknown error')}"


@mcp.tool()
def session_send(session: str, message: str) -> str:
    """Send a prompt/message to a session.

    Automatically includes the sender's session name so the receiving
    agent knows who sent the message and can reply via session_send.

    Args:
        session: Session name (can include @machine suffix for remote)
        message: The message to send (Enter key is appended automatically)

    Returns:
        Success message or error description.
    """
    caller = get_caller_session()
    if caller and caller != session:
        message = f"[From: {caller}]\n{message}"
    args = ["send", "-s", session, message]
    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Message sent to session '{session}'."
    return f"Failed to send message: {data.get('error', 'Unknown error')}"


@mcp.tool()
def session_output(session: str, lines: int = 50) -> str:
    """Capture output from a session.

    Args:
        session: Session name (can include @machine suffix for remote)
        lines: Number of lines to capture (default: 50)

    Returns:
        The captured output from the session.
    """
    args = ["output", "-s", session, "-n", str(lines)]
    data = run_agentwire_cmd(args)
    if data.get("success"):
        return data.get("output", "")
    return f"Failed to capture output: {data.get('error', 'Unknown error')}"


@mcp.tool()
def session_info(session: str) -> str:
    """Get detailed information about a session.

    Args:
        session: Session name (can include @machine suffix for remote)

    Returns:
        Session metadata including working directory, pane count, etc.
    """
    args = ["info", "-s", session]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to get session info: {data.get('error', 'Unknown error')}"

    # Format the info nicely
    lines = [f"Session: {session}"]
    if cwd := data.get("cwd"):
        lines.append(f"  Working directory: {cwd}")
    if panes := data.get("panes"):
        lines.append(f"  Panes: {len(panes)}")
    if session_type := data.get("type"):
        lines.append(f"  Type: {session_type}")
    if roles := data.get("roles"):
        lines.append(f"  Roles: {', '.join(roles)}")

    return "\n".join(lines)


@mcp.tool()
def session_kill(session: str) -> str:
    """Terminate a session.

    Args:
        session: Session name (can include @machine suffix for remote)

    Returns:
        Success message or error description.
    """
    args = ["kill", "-s", session]
    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Session '{session}' terminated."
    return f"Failed to kill session: {data.get('error', 'Unknown error')}"


# =============================================================================
# Pane Management Tools
# =============================================================================


@mcp.tool()
def pane_spawn(
    session: str | None = None,
    roles: str | None = None,
    pane_type: str | None = None,
) -> str:
    """Spawn a worker pane in a session.

    Workers share the orchestrator's working directory. For isolated commits
    with git worktrees, use CLI: agentwire spawn --branch <name>

    Args:
        session: Session name (defaults to current session if in tmux)
        roles: Comma-separated list of roles for the worker
        pane_type: Session type like 'claude-bypass' (optional)

    Returns:
        Pane index of the spawned worker or error description.
    """
    args = ["spawn"]

    if session:
        args.extend(["-s", session])
    if roles:
        args.extend(["--roles", roles])
    if pane_type:
        args.extend(["--type", pane_type])

    # Spawn can take a while to initialize the agent, use longer timeout
    data = run_agentwire_cmd(args, timeout=120)
    if data.get("success"):
        pane_idx = data.get("pane_index", data.get("pane", "?"))
        return f"Worker pane {pane_idx} spawned successfully."
    return f"Failed to spawn pane: {data.get('error', 'Unknown error')}"


@mcp.tool()
def pane_send(pane: int, message: str, session: str | None = None) -> str:
    """Send a message to a specific pane.

    Args:
        pane: Pane index (0 = orchestrator, 1+ = workers)
        message: The message to send
        session: Session name (defaults to current session if in tmux)

    Returns:
        Success message or error description.
    """
    args = ["send", "--pane", str(pane), message]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Message sent to pane {pane}."
    return f"Failed to send to pane: {data.get('error', 'Unknown error')}"


@mcp.tool()
def pane_output(pane: int, session: str | None = None, lines: int = 50) -> str:
    """Capture output from a specific pane.

    Args:
        pane: Pane index
        session: Session name (defaults to current session if in tmux)
        lines: Number of lines to capture (default: 50)

    Returns:
        The captured output from the pane.
    """
    args = ["output", "--pane", str(pane), "-n", str(lines)]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return data.get("output", "")
    return f"Failed to capture pane output: {data.get('error', 'Unknown error')}"


@mcp.tool()
def pane_kill(pane: int, session: str | None = None) -> str:
    """Kill a specific pane.

    Args:
        pane: Pane index to kill
        session: Session name (defaults to current session if in tmux)

    Returns:
        Success message or error description.
    """
    args = ["kill", "--pane", str(pane)]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Pane {pane} terminated."
    return f"Failed to kill pane: {data.get('error', 'Unknown error')}"


@mcp.tool()
def panes_list(session: str | None = None) -> str:
    """List panes in a session.

    Args:
        session: Session name (defaults to current session if in tmux)

    Returns:
        List of panes with their indices, commands, and status.
    """
    # Use 'info' command which returns pane information
    args = ["info"]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to list panes: {data.get('error', 'Unknown error')}"

    # Extract panes from info response
    panes = data.get("panes", [])
    session_name = session or data.get("session", "current")

    if not panes:
        return f"No panes found in session '{session_name}'."

    lines = [f"Panes in session '{session_name}':"]
    for p in panes:
        idx = p.get("index", 0)
        cmd = p.get("command", "unknown")
        active = " (active)" if p.get("active") else ""
        role = "orchestrator" if idx == 0 else "worker"
        lines.append(f"  - Pane {idx} [{role}]: {cmd}{active}")

    return "\n".join(lines)


# =============================================================================
# Machine Management Tools
# =============================================================================


@mcp.tool()
def machines_list() -> str:
    """List all configured remote machines.

    Returns:
        List of machines with their connection details and status.
    """
    data = run_agentwire_cmd(["machine", "list"])
    if not data.get("success"):
        return f"Failed to list machines: {data.get('error', 'Unknown error')}"
    return format_machines(data)


@mcp.tool()
def machine_add(machine_id: str, host: str, user: str, port: int = 22) -> str:
    """Add a new remote machine.

    Args:
        machine_id: Unique identifier for the machine
        host: Hostname or IP address
        user: SSH username
        port: SSH port (default: 22)

    Returns:
        Success message or error description.
    """
    args = ["machine", "add", machine_id, "--host", host, "--user", user]
    if port != 22:
        args.extend(["--port", str(port)])

    # machine add doesn't support --json
    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Machine '{machine_id}' added successfully."
    return f"Failed to add machine: {data.get('error', 'Unknown error')}"


@mcp.tool()
def machine_remove(machine_id: str) -> str:
    """Remove a remote machine.

    Args:
        machine_id: Machine identifier to remove

    Returns:
        Success message or error description.
    """
    args = ["machine", "remove", machine_id]
    # machine remove doesn't support --json
    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Machine '{machine_id}' removed."
    return f"Failed to remove machine: {data.get('error', 'Unknown error')}"


# =============================================================================
# Voice Tools (TTS/STT)
# =============================================================================


@mcp.tool()
def say(text: str, session: str | None = None, voice: str | None = None) -> str:
    """Speak text via TTS.

    Audio routes to the browser portal if connected, otherwise local speakers.

    Args:
        text: Text to speak
        session: Target session for audio routing (optional)
        voice: Voice name to use (optional, uses default if not specified)

    Returns:
        Success message or error description.
    """
    # Quick TTS health check — fail fast if server is unreachable
    try:
        from .config import load_config as load_typed_config
        from .network import NetworkContext
        import urllib.request

        cfg = load_typed_config()
        if cfg.tts.backend not in ("runpod", "none"):
            ctx = NetworkContext.from_config()
            tts_url = ctx.get_service_url("tts", use_tunnel=True)
            urllib.request.urlopen(f"{tts_url}/health", timeout=3)
    except Exception as e:
        url = locals().get("tts_url", "unknown")
        return f"TTS server unreachable at {url}: {e}"

    args = ["say"]
    if session:
        args.extend(["-s", session])
    if voice:
        args.extend(["--voice", voice])
    args.append(text)

    # Say command doesn't return JSON, run without --json
    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        from .utils.chunker import chunk_text
        chunks = chunk_text(text)
        if len(chunks) > 1:
            return f"Queued speech ({len(chunks)} chunks)."
        return "Queued speech."
    return f"Failed to speak: {data.get('error', 'Unknown error')}"


@mcp.tool()
def alert(text: str, to: str | None = None) -> str:
    """Send a text notification (no audio).

    Notifications appear in the target session/portal without voice.

    Args:
        text: Notification text
        to: Target session name (optional)

    Returns:
        Success message or error description.
    """
    args = ["alert"]
    if to:
        args.extend(["--to", to])
    args.append(text)

    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return "Alert sent."
    return f"Failed to send alert: {data.get('error', 'Unknown error')}"


@mcp.tool()
def listen_start() -> str:
    """Start voice recording.

    Begins recording audio for speech-to-text transcription.
    Call listen_stop() to stop and get the transcript.

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["listen", "start"], json_output=False)
    if data.get("success"):
        return "Recording started."
    return f"Failed to start recording: {data.get('error', 'Unknown error')}"


@mcp.tool()
def listen_stop() -> str:
    """Stop recording and get transcript.

    Stops the current recording and transcribes the audio.

    Returns:
        The transcribed text or error description.
    """
    # listen stop doesn't support --json, run without it
    data = run_agentwire_cmd(["listen", "stop"], json_output=False)
    if data.get("success"):
        return data.get("output", "Recording stopped.")
    return f"Failed to stop recording: {data.get('error', 'Unknown error')}"


@mcp.tool()
def transcribe(audio_base64: str, format: str = "webm") -> str:
    """Transcribe audio to text.

    Accepts base64-encoded audio data and returns the transcribed text.
    This is useful for external agents that have their own audio capture
    or want to process pre-recorded audio files.

    Args:
        audio_base64: Base64-encoded audio data
        format: Audio format - webm, wav, mp3, ogg, m4a (default: webm)

    Returns:
        Transcribed text or error description.
    """
    import requests
    import urllib3

    # Suppress SSL warnings for self-signed certs
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Decode base64 audio
    try:
        audio_bytes = base64.b64decode(audio_base64)
    except Exception as e:
        return f"Failed to decode base64 audio: {e}"

    if not audio_bytes:
        return "Empty audio data"

    # Determine MIME type
    mime_types = {
        "webm": "audio/webm",
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "ogg": "audio/ogg",
        "m4a": "audio/m4a",
    }
    mime_type = mime_types.get(format.lower(), "audio/webm")

    # POST to portal's /transcribe endpoint
    portal_url = get_portal_url()
    url = f"{portal_url}/transcribe"

    try:
        # Create multipart form data
        files = {"audio": (f"audio.{format}", audio_bytes, mime_type)}
        response = requests.post(url, files=files, verify=False, timeout=60)

        if response.status_code != 200:
            return f"Transcription request failed: HTTP {response.status_code}"

        data = response.json()
        if "error" in data:
            return f"Transcription failed: {data['error']}"

        return data.get("text", "")

    except requests.exceptions.ConnectionError:
        return "Failed to connect to portal. Is it running? (agentwire portal status)"
    except Exception as e:
        return f"Transcription failed: {e}"


@mcp.tool()
def voices_list() -> str:
    """List available TTS voices.

    Returns:
        List of voice names that can be used with the say() tool.
    """
    data = run_agentwire_cmd(["voiceclone", "list"])
    if not data.get("success"):
        return f"Failed to list voices: {data.get('error', 'Unknown error')}"
    return format_voices(data)


# =============================================================================
# Projects & Roles Tools
# =============================================================================


@mcp.tool()
def projects_list() -> str:
    """Discover available projects.

    Scans the configured projects directory for projects that can
    be used to create new sessions.

    Returns:
        List of projects with their paths and configuration status.
    """
    data = run_agentwire_cmd(["projects", "list"])
    if not data.get("success"):
        return f"Failed to list projects: {data.get('error', 'Unknown error')}"
    return format_projects(data)


@mcp.tool()
def roles_list() -> str:
    """List available roles.

    Roles define agent behavior and capabilities. They can be applied
    when creating sessions or spawning workers.

    Returns:
        List of roles with their descriptions.
    """
    data = run_agentwire_cmd(["roles", "list"])
    if not data.get("success"):
        return f"Failed to list roles: {data.get('error', 'Unknown error')}"
    return format_roles(data)


@mcp.tool()
def role_show(name: str) -> str:
    """Get detailed information about a role.

    Args:
        name: Role name to look up

    Returns:
        Role details including description, tools, and instructions.
    """
    data = run_agentwire_cmd(["roles", "show", name])
    if not data.get("success"):
        return f"Failed to show role: {data.get('error', 'Unknown error')}"

    lines = [f"Role: {name}"]
    if desc := data.get("description"):
        lines.append(f"  Description: {desc}")
    if tools := data.get("tools"):
        lines.append(f"  Tools: {', '.join(tools)}")
    if model := data.get("model"):
        lines.append(f"  Model: {model}")
    if instructions := data.get("instructions"):
        # Truncate long instructions
        preview = instructions[:200] + "..." if len(instructions) > 200 else instructions
        lines.append(f"  Instructions: {preview}")

    return "\n".join(lines)


# =============================================================================
# Status Tools
# =============================================================================


@mcp.tool()
def portal_status() -> str:
    """Check portal server health.

    Returns:
        Portal status including whether it's running and on what port.
    """
    data = run_agentwire_cmd(["portal", "status"])
    if data.get("success"):
        running = data.get("running", False)
        url = data.get("url", get_portal_url())
        if running:
            return f"Portal is running at {url}"
        return "Portal is not running. Start with 'agentwire portal start'."
    return f"Failed to check portal status: {data.get('error', 'Unknown error')}"


@mcp.tool()
def tts_status() -> str:
    """Check TTS server status.

    Returns:
        TTS server status and configuration.
    """
    data = run_agentwire_cmd(["tts", "status"])
    if data.get("success"):
        running = data.get("running", False)
        backend = data.get("backend", "unknown")
        if running:
            return f"TTS server is running (backend: {backend})"
        return f"TTS server is not running. Backend configured: {backend}"
    return f"Failed to check TTS status: {data.get('error', 'Unknown error')}"


@mcp.tool()
def stt_status() -> str:
    """Check STT server status.

    Returns:
        STT server status and configuration.
    """
    data = run_agentwire_cmd(["stt", "status"])
    if data.get("success"):
        running = data.get("running", False)
        if running:
            return "STT server is running."
        return "STT server is not running."
    return f"Failed to check STT status: {data.get('error', 'Unknown error')}"


# =============================================================================
# Task Tools (Scheduled Workloads)
# =============================================================================


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


# =============================================================================
# Session Management (Extended)
# =============================================================================


@mcp.tool()
def session_send_keys(session: str, keys: list[str]) -> str:
    """Send raw keys to a session without automatic Enter.

    Useful for sending control sequences like Ctrl-C, Escape, Enter,
    arrow keys, etc. Each key group is sent with a brief pause between.

    Args:
        session: Session name (can include @machine suffix for remote)
        keys: List of key groups to send (e.g., ["Ctrl-C", "Enter"])

    Returns:
        Success message or error description.
    """
    args = ["send-keys", "-s", session] + keys
    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Sent {len(keys)} key group(s) to '{session}'."
    return f"Failed to send keys: {data.get('error', 'Unknown error')}"


@mcp.tool()
def session_recreate(session: str) -> str:
    """Destroy and recreate a session with a fresh worktree.

    Useful when a session is in a bad state and needs a clean start.

    Args:
        session: Session name to recreate

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["recreate", "-s", session], timeout=180)
    if data.get("success"):
        return f"Session '{session}' recreated with fresh worktree."
    return f"Failed to recreate session: {data.get('error', 'Unknown error')}"


@mcp.tool()
def session_fork(session: str, target: str, commit: str = "") -> str:
    """Fork a session into a new worktree.

    Creates a new session based on an existing one, with its own
    git worktree for isolated work.

    Args:
        session: Source session name (project or project/branch)
        target: Target session name (must include branch: project/new-branch)
        commit: Optional commit/ref to fork from instead of HEAD (e.g. abc123, main~5)

    Returns:
        Success message or error description.
    """
    cmd = ["fork", "-s", session, "-t", target]
    if commit:
        cmd += ["--commit", commit]
    data = run_agentwire_cmd(cmd, timeout=120)
    if data.get("success"):
        forked = data.get("session", target)
        return f"Session '{session}' forked to '{forked}'."
    return f"Failed to fork session: {data.get('error', 'Unknown error')}"


# =============================================================================
# Pane Layout Tools
# =============================================================================


@mcp.tool()
def pane_split(session: str | None = None, count: int = 1) -> str:
    """Add terminal pane(s) to a session with even vertical layout.

    Args:
        session: Session name (defaults to current session if in tmux)
        count: Number of panes to add (default: 1)

    Returns:
        Success message or error description.
    """
    args = ["split", "-n", str(count)]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Added {count} terminal pane(s)."
    return f"Failed to split panes: {data.get('error') or data.get('output') or 'Unknown error'}"


@mcp.tool()
def pane_detach(session: str, pane: int, target: str) -> str:
    """Move a pane to its own session.

    Detaches a pane from its current session and creates a new
    session for it.

    Args:
        session: Source session name
        pane: Pane index to detach
        target: Target session name (created if doesn't exist)

    Returns:
        Success message or error description.
    """
    args = ["detach", "--pane", str(pane), "-s", target, "--source", session]
    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Pane {pane} detached from '{session}' to '{target}'."
    return f"Failed to detach pane: {data.get('error') or data.get('output') or 'Unknown error'}"


@mcp.tool()
def pane_jump(session: str | None = None, pane: int = 0) -> str:
    """Focus a specific pane in tmux.

    Args:
        session: Session name (defaults to current session if in tmux)
        pane: Pane index to focus (default: 0)

    Returns:
        Success message or error description.
    """
    args = ["jump", "--pane", str(pane)]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Focused pane {pane}."
    return f"Failed to focus pane: {data.get('error', 'Unknown error')}"


@mcp.tool()
def pane_resize(session: str | None = None) -> str:
    """Resize tmux window to fit the largest client.

    Args:
        session: Session name (defaults to current session if in tmux)

    Returns:
        Success message or error description.
    """
    args = ["resize"]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return "Window resized to fit largest client."
    return f"Failed to resize: {data.get('error', 'Unknown error')}"


# =============================================================================
# Voice Cloning Tools
# =============================================================================


@mcp.tool()
def voiceclone_start() -> str:
    """Start recording a voice sample for cloning.

    Records audio from the microphone to create a custom TTS voice.
    Call voiceclone_stop() with a name to save, or voiceclone_cancel() to discard.

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["voiceclone", "start"], json_output=False)
    if data.get("success"):
        return "Voice recording started. Speak clearly for 10-30 seconds, then call voiceclone_stop() with a name."
    return f"Failed to start recording: {data.get('error', 'Unknown error')}"


@mcp.tool()
def voiceclone_stop(name: str) -> str:
    """Stop recording and save as a named voice clone.

    Args:
        name: Name for the cloned voice (used with say() voice parameter)

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["voiceclone", "stop", name], json_output=False)
    if data.get("success"):
        return f"Voice clone '{name}' saved. Use with: say(text='...', voice='{name}')"
    return f"Failed to save voice clone: {data.get('error', 'Unknown error')}"


@mcp.tool()
def voiceclone_cancel() -> str:
    """Cancel the current voice recording without saving.

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["voiceclone", "cancel"], json_output=False)
    if data.get("success"):
        return "Voice recording cancelled."
    return f"Failed to cancel recording: {data.get('error', 'Unknown error')}"


@mcp.tool()
def voiceclone_list() -> str:
    """List all cloned voices.

    Returns:
        List of cloned voice names that can be used with say().
    """
    data = run_agentwire_cmd(["voiceclone", "list"])
    if not data.get("success"):
        return f"Failed to list voice clones: {data.get('error', 'Unknown error')}"
    return format_voices(data)


@mcp.tool()
def voiceclone_delete(name: str) -> str:
    """Delete a cloned voice.

    Args:
        name: Name of the voice clone to delete

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["voiceclone", "delete", name], json_output=False)
    if data.get("success"):
        return f"Voice clone '{name}' deleted."
    return f"Failed to delete voice clone: {data.get('error', 'Unknown error')}"


# =============================================================================
# History Tools
# =============================================================================


@mcp.tool()
def history_list(project: str | None = None, limit: int = 20) -> str:
    """List conversation history for sessions.

    Args:
        project: Filter by project path (optional)
        limit: Maximum number of results (default: 20)

    Returns:
        List of past sessions with IDs and timestamps.
    """
    args = ["history", "list", "-n", str(limit)]
    if project:
        args.extend(["--project", project])

    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to list history: {data.get('error', 'Unknown error')}"

    # CLI returns a JSON array, which run_agentwire_cmd wraps as {"items": [...]}
    sessions = data.get("items", data.get("sessions", []))
    if not sessions:
        return "No session history found."

    lines = ["Session history:"]
    for s in sessions:
        sid = s.get("sessionId", s.get("id", "unknown"))
        first_msg = s.get("firstMessage", "")
        count = s.get("messageCount", 0)
        preview = (first_msg[:60] + "...") if len(first_msg) > 60 else first_msg
        lines.append(f"  - {sid}: {preview} ({count} messages)")

    return "\n".join(lines)


@mcp.tool()
def history_show(session_id: str) -> str:
    """Show details of a past session.

    Args:
        session_id: Session ID from history_list

    Returns:
        Session details including commands and duration.
    """
    data = run_agentwire_cmd(["history", "show", session_id])
    if not data.get("success"):
        return f"Failed to show session: {data.get('error', 'Unknown error')}"

    lines = [f"Session: {data.get('sessionId', session_id)}"]
    if first_msg := data.get("firstMessage"):
        preview = (first_msg[:80] + "...") if len(first_msg) > 80 else first_msg
        lines.append(f"  First message: {preview}")
    if branch := data.get("gitBranch"):
        lines.append(f"  Branch: {branch}")
    if count := data.get("messageCount"):
        lines.append(f"  Messages: {count}")
    if timestamps := data.get("timestamps"):
        if start := timestamps.get("start"):
            from datetime import datetime
            lines.append(f"  Started: {datetime.fromtimestamp(start / 1000).strftime('%Y-%m-%d %H:%M')}")
    if summaries := data.get("summaries"):
        lines.append(f"  Summaries: {len(summaries)}")

    return "\n".join(lines)


@mcp.tool()
def history_resume(session_id: str, project: str) -> str:
    """Resume a past session (always creates a fork).

    Args:
        session_id: Session ID from history_list
        project: Project path for the resumed session

    Returns:
        Success message with new session name or error.
    """
    data = run_agentwire_cmd(
        ["history", "resume", session_id, "--project", project],
        timeout=120,
    )
    if data.get("success"):
        new_session = data.get("session", "unknown")
        return f"Session resumed as '{new_session}'."
    return f"Failed to resume session: {data.get('error', 'Unknown error')}"


# =============================================================================
# Lock Management Tools
# =============================================================================


@mcp.tool()
def lock_list() -> str:
    """List all active task locks.

    Returns:
        List of locks with session names and timestamps.
    """
    data = run_agentwire_cmd(["lock", "list"])
    if not data.get("success"):
        return f"Failed to list locks: {data.get('error', 'Unknown error')}"

    locks = data.get("locks", [])
    if not locks:
        return "No active locks."

    lines = ["Active locks:"]
    for lock in locks:
        session = lock.get("session", "unknown")
        acquired = lock.get("acquired", "")
        pid = lock.get("pid", "")
        lines.append(f"  - {session}: acquired {acquired} (pid: {pid})")

    return "\n".join(lines)


@mcp.tool()
def lock_clean() -> str:
    """Remove stale locks (from dead processes).

    Returns:
        Number of stale locks removed or error.
    """
    data = run_agentwire_cmd(["lock", "clean"])
    if data.get("success"):
        removed = data.get("removed", [])
        count = data.get("count", len(removed) if isinstance(removed, list) else removed)
        if isinstance(removed, list) and removed:
            return f"Cleaned {count} stale lock(s): {', '.join(removed)}"
        return f"Cleaned {count} stale lock(s)."
    return f"Failed to clean locks: {data.get('error', 'Unknown error')}"


@mcp.tool()
def lock_remove(session: str) -> str:
    """Force-remove a specific lock.

    Args:
        session: Session name whose lock to remove

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["lock", "remove", session])
    if data.get("success"):
        return f"Lock for '{session}' removed."
    return f"Failed to remove lock: {data.get('error', 'Unknown error')}"


# =============================================================================
# Scheduler Tools
# =============================================================================


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
        artifact: If True, open the report as a portal artifact window

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


# =============================================================================
# Notification Tools
# =============================================================================


@mcp.tool()
def email_send(
    body: str,
    to: str | None = None,
    subject: str | None = None,
    attachments: list[str] | None = None,
    plain_text: bool = False,
) -> str:
    """Send a branded email notification via Resend.

    Supports markdown in the body. Uses the HTML email template.

    Args:
        body: Email body (markdown supported)
        to: Recipient email address (default: from config)
        subject: Email subject line (optional)
        attachments: List of file paths to attach (optional)
        plain_text: Send plain text only, no HTML template (default: false)

    Returns:
        Success message or error description.
    """
    args = ["email", "--body", body]
    if to:
        args.extend(["--to", to])
    if subject:
        args.extend(["--subject", subject])
    if attachments:
        for path in attachments:
            args.extend(["--attach", path])
    if plain_text:
        args.append("--plain")

    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return "Email sent."
    return f"Failed to send email: {data.get('error', 'Unknown error')}"


@mcp.tool()
def session_notify(event: str, session: str | None = None) -> str:
    """Notify portal of session/pane state changes.

    Args:
        event: Event type (e.g., 'session_idle', 'session_active')
        session: Session name (optional, auto-detected if in tmux)

    Returns:
        Success message or error description.
    """
    args = ["notify", event]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Notification '{event}' sent."
    return f"Failed to send notification: {data.get('error', 'Unknown error')}"


# =============================================================================
# Tunnel Tools
# =============================================================================


@mcp.tool()
def tunnels_up() -> str:
    """Create all required SSH tunnels for remote services.

    Reads tunnel requirements from config and creates SSH tunnels
    to reach remote services (TTS, portal, etc.).

    Returns:
        Status of tunnel creation.
    """
    data = run_agentwire_cmd(["tunnels", "up"], json_output=False, timeout=60)
    if data.get("success"):
        return data.get("output", "Tunnels created.")
    return f"Failed to create tunnels: {data.get('error', 'Unknown error')}"


@mcp.tool()
def tunnels_down() -> str:
    """Tear down all SSH tunnels.

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["tunnels", "down"], json_output=False)
    if data.get("success"):
        return data.get("output", "Tunnels torn down.")
    return f"Failed to tear down tunnels: {data.get('error', 'Unknown error')}"


@mcp.tool()
def tunnels_status() -> str:
    """Show SSH tunnel health.

    Returns:
        Status of all configured tunnels.
    """
    data = run_agentwire_cmd(["tunnels", "status"], json_output=False)
    if data.get("success"):
        return data.get("output", "No tunnels configured.")
    return f"Failed to check tunnel status: {data.get('error', 'Unknown error')}"


# =============================================================================
# Listen Tools (Extended)
# =============================================================================


@mcp.tool()
def listen_cancel() -> str:
    """Cancel the current voice recording without transcribing.

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["listen", "cancel"], json_output=False)
    if data.get("success"):
        return "Recording cancelled."
    return f"Failed to cancel recording: {data.get('error', 'Unknown error')}"


# =============================================================================
# Task Tools (Extended)
# =============================================================================


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


# =============================================================================
# Network Tools
# =============================================================================


@mcp.tool()
def network_status() -> str:
    """Show complete network health at a glance.

    Checks machine connectivity, service health, and tunnel status.
    Note: exits non-zero when issues are detected, but the output is still useful.

    Returns:
        Network status report.
    """
    data = run_agentwire_cmd(["network", "status"], json_output=False, timeout=60)
    output = data.get("output", "")
    if output:
        return output
    return f"Failed to check network: {data.get('error', 'Unknown error')}"


# =============================================================================
# Desktop/Portal UI Control Tools
# =============================================================================


def _portal_request(method: str, path: str, body: dict | None = None) -> dict:
    """Make an HTTP request to the portal API.

    Args:
        method: HTTP method (GET or POST)
        path: API path (e.g., /api/desktop/windows)
        body: Request body for POST requests

    Returns:
        Response data as dict.
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    url = f"{get_portal_url()}{path}"
    try:
        if method == "GET":
            resp = requests.get(url, verify=False, timeout=10)
        elif method == "DELETE":
            resp = requests.delete(url, verify=False, timeout=10)
        else:
            resp = requests.post(url, json=body or {}, verify=False, timeout=10)

        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}"}
        return resp.json()
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "Portal not reachable. Is it running?"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def desktop_windows_list() -> str:
    """List all open windows in the portal desktop.

    Returns:
        List of open windows with IDs, types, and positions.
    """
    data = _portal_request("GET", "/api/desktop/windows")
    if not data.get("success", True):
        return f"Failed to list windows: {data.get('error', 'Unknown error')}"

    windows = data.get("windows", [])
    if not windows:
        return "No windows open."

    lines = ["Open windows:"]
    for w in windows:
        wid = w.get("id", "unknown")
        wtype = w.get("type", "unknown")
        title = w.get("title", "")
        zone = w.get("zone", "")
        zone_str = f" [{zone}]" if zone else ""
        lines.append(f"  - {wid}: {title} ({wtype}){zone_str}")

    return "\n".join(lines)


@mcp.tool()
def desktop_open_session(session: str, mode: str = "monitor") -> str:
    """Open a session window in the portal desktop.

    Args:
        session: Session name to open
        mode: Window mode - 'monitor' (read-only) or 'terminal' (interactive)

    Returns:
        Window ID of the opened window or error.
    """
    data = _portal_request("POST", "/api/desktop/window/open", {
        "type": "session",
        "session": session,
        "mode": mode,
    })
    if data.get("success"):
        wid = data.get("window_id", "unknown")
        return f"Opened {mode} window for '{session}' (id: {wid})."
    return f"Failed to open window: {data.get('error', 'Unknown error')}"


@mcp.tool()
def desktop_open_panel(panel_type: str) -> str:
    """Open a panel window in the portal desktop.

    Args:
        panel_type: Panel to open - 'sessions', 'machines', 'projects', 'artifacts', or 'config'

    Returns:
        Window ID of the opened panel or error.
    """
    data = _portal_request("POST", "/api/desktop/window/open", {
        "type": "panel",
        "panel": panel_type,
    })
    if data.get("success"):
        wid = data.get("window_id", "unknown")
        return f"Opened '{panel_type}' panel (id: {wid})."
    return f"Failed to open panel: {data.get('error', 'Unknown error')}"


@mcp.tool()
def desktop_open_artifact(url: str, title: str = "Artifact", artifact_id: str | None = None) -> str:
    """Open a URL or local artifact file in an iframe window on the portal desktop.

    For local files, use a filename from ~/.agentwire/artifacts/ (e.g., "dashboard.html").
    For external sites, use a full URL (e.g., "https://example.com").

    Args:
        url: URL or filename to display. Filenames are served from ~/.agentwire/artifacts/.
        title: Window title (default: "Artifact")
        artifact_id: Optional unique window ID. If omitted, derived from URL.

    Returns:
        Window ID of the opened window or error.
    """
    body = {
        "type": "artifact",
        "url": url,
        "title": title,
    }
    if artifact_id:
        body["artifact_id"] = artifact_id

    data = _portal_request("POST", "/api/desktop/window/open", body)
    if data.get("success"):
        wid = data.get("window_id", "unknown")
        return f"Opened artifact window '{title}' (id: {wid})."
    return f"Failed to open artifact window: {data.get('error', 'Unknown error')}"


@mcp.tool()
def desktop_write_artifact(
    filename: str,
    html_content: str,
    title: str = "Artifact",
    artifact_id: str | None = None,
) -> str:
    """Write HTML content to a file and open it as an artifact window.

    Atomically writes content to ~/.agentwire/artifacts/<filename>, then opens
    it in an iframe window on the portal desktop. Use this to display
    dashboards, diagrams, reports, or any HTML content.

    Args:
        filename: Output filename (must end in .html, e.g., "dashboard.html")
        html_content: Complete HTML content to write
        title: Window title (default: "Artifact")
        artifact_id: Optional unique window ID. If omitted, derived from filename.

    Returns:
        Window ID of the opened window or error.
    """
    # Step 1: Upload the file
    upload_data = _portal_request("POST", "/api/artifacts/upload", {
        "filename": filename,
        "content": html_content,
    })
    if not upload_data.get("success"):
        return f"Failed to write artifact: {upload_data.get('error', 'Unknown error')}"

    # Step 2: Open it as a window
    body = {
        "type": "artifact",
        "url": filename,
        "title": title,
    }
    if artifact_id:
        body["artifact_id"] = artifact_id

    open_data = _portal_request("POST", "/api/desktop/window/open", body)
    if open_data.get("success"):
        wid = open_data.get("window_id", "unknown")
        return f"Artifact '{filename}' written and opened (id: {wid})."
    return f"File written but failed to open window: {open_data.get('error', 'Unknown error')}"


@mcp.tool()
def desktop_close_window(window_id: str) -> str:
    """Close a window in the portal desktop.

    Args:
        window_id: Window ID from desktop_windows_list

    Returns:
        Success message or error description.
    """
    data = _portal_request("POST", "/api/desktop/window/close", {
        "window_id": window_id,
    })
    if data.get("success"):
        return f"Window '{window_id}' closed."
    return f"Failed to close window: {data.get('error', 'Unknown error')}"


@mcp.tool()
def desktop_focus_window(window_id: str) -> str:
    """Bring a window to the front in the portal desktop.

    Args:
        window_id: Window ID from desktop_windows_list

    Returns:
        Success message or error description.
    """
    data = _portal_request("POST", "/api/desktop/window/focus", {
        "window_id": window_id,
    })
    if data.get("success"):
        return f"Window '{window_id}' focused."
    return f"Failed to focus window: {data.get('error', 'Unknown error')}"


@mcp.tool()
def desktop_tile_window(window_id: str, zone: str) -> str:
    """Tile a window to a specific zone in the portal desktop.

    Args:
        window_id: Window ID from desktop_windows_list
        zone: Tile zone - 'left', 'right', 'top', 'bottom',
              'top-left', 'top-right', 'bottom-left', 'bottom-right'

    Returns:
        Success message or error description.
    """
    data = _portal_request("POST", "/api/desktop/window/tile", {
        "window_id": window_id,
        "zone": zone,
    })
    if data.get("success"):
        return f"Window '{window_id}' tiled to {zone}."
    return f"Failed to tile window: {data.get('error', 'Unknown error')}"


@mcp.tool()
def desktop_minimize_all() -> str:
    """Minimize all windows in the portal desktop.

    Returns:
        Success message or error description.
    """
    data = _portal_request("POST", "/api/desktop/window/minimize-all")
    if data.get("success"):
        return "All windows minimized."
    return f"Failed to minimize windows: {data.get('error', 'Unknown error')}"


@mcp.tool()
def desktop_layout(windows: list[dict]) -> str:
    """Apply a multi-window layout to the portal desktop.

    Tiles multiple windows at once for side-by-side or grid layouts.

    Args:
        windows: List of window placements, each with 'id' and 'zone' keys.
                 Example: [{"id": "win-1", "zone": "left"}, {"id": "win-2", "zone": "right"}]

    Returns:
        Success message or error description.
    """
    data = _portal_request("POST", "/api/desktop/layout", {
        "windows": windows,
    })
    if data.get("success"):
        return f"Layout applied to {len(windows)} window(s)."
    return f"Failed to apply layout: {data.get('error', 'Unknown error')}"


# =============================================================================
# SDK Hierarchy Tools
# =============================================================================


@mcp.tool()
def sdk_child_spawn(
    child_name: str,
    parent: str | None = None,
    path: str | None = None,
    role: str | None = None,
    auto_kill: bool = True,
) -> str:
    """Spawn a child SDK session linked to a parent.

    Creates a new SDK session that notifies the parent when it completes.
    The child inherits the parent's session type and working directory
    unless overridden.

    Args:
        child_name: Name for the child session (required)
        parent: Parent session name (defaults to caller's session)
        path: Working directory for the child (defaults to parent's path)
        role: Role name to load as instructions for the child (optional)
        auto_kill: Kill child automatically on completion (default True)

    Returns:
        Success message or error description.
    """
    parent_name = parent or get_caller_session()
    if not parent_name:
        return "Could not detect parent session. Provide 'parent' explicitly."

    data = _portal_request("POST", f"/api/session/{parent_name}/spawn", {
        "name": child_name,
        "path": path,
        "role": role,
        "auto_kill_on_complete": auto_kill,
    })
    if data.get("success"):
        return f"Child '{child_name}' spawned for parent '{parent_name}'."
    return f"Failed to spawn child: {data.get('error', 'Unknown error')}"


@mcp.tool()
def sdk_child_send(child: str, prompt: str) -> str:
    """Send a prompt to a child SDK session.

    Args:
        child: Child session name
        prompt: The prompt to send

    Returns:
        Success message or error description.
    """
    data = _portal_request("POST", f"/api/session/{child}/prompt", {
        "prompt": prompt,
    })
    if data.get("success"):
        return f"Prompt sent to child '{child}'."
    return f"Failed to send prompt: {data.get('error', 'Unknown error')}"


@mcp.tool()
def sdk_child_status(child: str) -> str:
    """Check if a child SDK session is busy or idle.

    Args:
        child: Child session name

    Returns:
        Status information (exists, busy, message_count).
    """
    data = _portal_request("GET", f"/api/session/{child}/status")
    if not data.get("exists"):
        return f"Child '{child}' does not exist."
    busy = "busy" if data.get("busy") else "idle"
    count = data.get("message_count", 0)
    return f"Child '{child}': {busy}, {count} messages."


@mcp.tool()
def sdk_child_result(child: str) -> str:
    """Get the last result from a child SDK session.

    Scans messages for the most recent ResultMessage.

    Args:
        child: Child session name

    Returns:
        Last result text or status message.
    """
    data = _portal_request("GET", f"/api/session/{child}/messages")
    messages = data.get("messages", [])
    # Find last result message
    for msg in reversed(messages):
        if msg.get("type") == "result":
            status = "error" if msg.get("is_error") else "complete"
            cost = f" (${msg['total_cost_usd']:.4f})" if msg.get("total_cost_usd") else ""
            result_text = msg.get("result", "")
            return f"[{status}]{cost} {result_text}"
    return f"No result found for child '{child}'."


@mcp.tool()
def sdk_children_list(parent: str | None = None) -> str:
    """List children of an SDK parent session.

    Args:
        parent: Parent session name (defaults to caller's session)

    Returns:
        List of children with their status.
    """
    parent_name = parent or get_caller_session()
    if not parent_name:
        return "Could not detect parent session. Provide 'parent' explicitly."

    data = _portal_request("GET", f"/api/session/{parent_name}/children")
    children = data.get("children", [])
    if not children:
        return f"No children for session '{parent_name}'."

    lines = [f"Children of '{parent_name}':"]
    for c in children:
        busy = "busy" if c.get("busy") else "idle"
        lines.append(f"  - {c['name']}: {busy}, {c.get('message_count', 0)} messages, path={c.get('path', '')}")
    return "\n".join(lines)


@mcp.tool()
def sdk_child_kill(child: str) -> str:
    """Kill a child SDK session.

    Args:
        child: Child session name to kill

    Returns:
        Success message or error description.
    """
    data = _portal_request("DELETE", f"/api/sessions/{child}")
    if data.get("success"):
        return f"Child '{child}' killed."
    return f"Failed to kill child: {data.get('error', 'Unknown error')}"


# =============================================================================
# Server Entry Point
# =============================================================================


def run_server():
    """Run the MCP server on stdio transport."""
    logger.info("Starting AgentWire MCP server")
    logger.info(f"Portal URL: {get_portal_url()}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_server()
