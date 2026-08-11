"""MCP tools — services domain."""

from .core import run_agentwire_cmd
from .mcp_core import (
    mcp,
)


@mcp.tool()
def services_list() -> str:
    """List registered custom services (long-running registered sessions).

    Custom services autostart on portal launch / `agentwire up` and are
    health-checked by the portal watchdog. Includes the built-in
    notifications bridge plus services.custom entries from config.

    Returns:
        Each service with its project, restart policy, healthcheck, and flags.
    """
    data = run_agentwire_cmd(["services", "list"])
    if not data.get("success"):
        return f"Failed to list services: {data.get('error', 'Unknown error')}"
    services = data.get("services", [])
    if not services:
        return "No custom services registered."
    lines = []
    for s in services:
        flags = []
        if not s.get("autostart"):
            flags.append("autostart off")
        if s.get("disabled"):
            flags.append("disabled")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        hc = s.get("healthcheck", {})
        lines.append(f"- {s['name']} ({s.get('kind')}): restart={s.get('restart')}, "
                     f"healthcheck={hc.get('kind')}/{hc.get('interval')}s{suffix}")
        if s.get("command"):
            lines.append(f"  command: {s['command']}")
        if s.get("project"):
            lines.append(f"  project: {s['project']}")
    return "\n".join(lines)


@mcp.tool()
def services_status() -> str:
    """Health status for all custom services (runs healthchecks now).

    Returns:
        Per-service health with detail; flags services that should be
        running but aren't.
    """
    data = run_agentwire_cmd(["services", "status"])
    if not data.get("success"):
        return f"Failed to get services status: {data.get('error', 'Unknown error')}"
    statuses = data.get("services", [])
    if not statuses:
        return "No custom services registered."
    lines = []
    for s in statuses:
        if s.get("healthy"):
            mark = "ok"
        elif s.get("disabled") or not s.get("autostart"):
            mark = ".."
        else:
            mark = "!!"
        extra = " (disabled)" if s.get("disabled") else (
            "" if s.get("autostart") else " (autostart off)")
        lines.append(f"[{mark}] {s['name']} ({s.get('kind')}): {s.get('detail')}{extra}")
    all_healthy = data.get("all_healthy")
    lines.append(f"\nAll healthy: {'yes' if all_healthy else 'NO'}")
    return "\n".join(lines)
