"""Shared MCP server foundation.

Holds the singleton ``mcp = FastMCP(...)`` instance plus the cross-domain
helpers (CLI runner, result formatters) that every ``mcp_*`` domain module
imports. Mirrors ``core.py`` for the CLI split (#495).
"""

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


def get_portal_url() -> str:
    """Get portal URL from environment or config.

    Resolution order:
    1. AGENTWIRE_PORTAL_URL env var
    2. ~/.agentwire/config.yaml → portal.url
    3. Default: localhost:8765 (https when SSL certs exist, else http)
    """
    # 1. Environment variable
    if url := os.environ.get("AGENTWIRE_PORTAL_URL"):
        return url

    # 2. Config file
    config = {}
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

    # 3. Default — https only when server.ssl cert/key are configured AND
    # exist (mirrors the typed config's scheme logic)
    ssl_cfg = config.get("server", {}).get("ssl", {})
    cert, key = ssl_cfg.get("cert"), ssl_cfg.get("key")
    enabled = bool(
        cert and key
        and Path(os.path.expanduser(cert)).exists()
        and Path(os.path.expanduser(key)).exists()
    )
    return f"{'https' if enabled else 'http'}://localhost:8765"


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


def _delivery_result(data: dict, where: str) -> str:
    """Honest delivery report for send/notify tools (#444).

    The CLI confirms a paste actually landed in the pane (``--verify``) and
    returns ``verified``: True (landed), False (sent but not seen — likely a
    busy/booting pane that dropped it), or None (remote — unverifiable across
    SSH). Surface that instead of a blind "sent".
    """
    verified = data.get("verified")
    if verified is True:
        return f"Message delivered {where} (verified in pane)."
    if verified is False:
        return (f"Message sent {where} but delivery could NOT be verified — it may "
                f"have been dropped (busy/booting pane). Check the pane or resend.")
    if verified is None and "verified" in data:
        return f"Message sent {where} (remote session — delivery can't be verified across SSH)."
    return f"Message sent {where}."


def _mcp_result(data: dict, on_success: str, operation: str = "complete operation") -> str:
    """Standard success/error string for a thin MCP wrapper over run_agentwire_cmd.

    Collapses the repeated `if data.get("success"): return X; return f"Failed…"`
    pattern so every wrapper reports failures the same way.
    """
    if data.get("success"):
        return on_success
    return f"Failed to {operation}: {data.get('error', 'Unknown error')}"


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
        parked = " [PARKED: usage limit, auto-resumes after reset]" if s.get("usage_limit") else ""
        lines.append(f"  - {name} ({machine}): {windows} window(s), type={session_type}, path={path}{parked}")

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
