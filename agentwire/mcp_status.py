"""MCP tools — status domain."""

from .mcp_core import (
    get_portal_url,
    mcp,
    run_agentwire_cmd,
)


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


def _format_voice_status(kind: str, data: dict) -> str:
    """Render a voice_status resolver payload (from `tts/stt status --json`).

    Answers "can I X right now, and via what path" instead of probing a server
    the active tier may never use. Surfaces orphaned engine servers (running
    but unused by the tier).
    """
    if not data.get("success"):
        return f"Failed to check {kind} status: {data.get('error', 'Unknown error')}"
    tier = data.get("tier", "unknown")
    path = data.get("path", "?")
    ready = data.get("ready", False)
    detail = data.get("detail", "")
    head = "ready" if ready else "NOT ready"
    lines = [f"{kind} [{tier} tier] — {head}: {path}"]
    if detail:
        lines.append(f"  {detail}")
    for w in data.get("warnings") or []:
        lines.append(f"  ⚠ {w}")
    return "\n".join(lines)


@mcp.tool()
def tts_status() -> str:
    """Can I speak right now, and through what path?

    Reports the ACTIVE TTS tier (default → browser portal when connected, else
    OS voice; custom → shim) — not a probe of a configured-but-unused server.
    Flags an orphaned engine server (e.g. a shim still up on the custom port
    while the tier is 'default').

    Returns:
        Active TTS path, readiness, and any orphan warnings.
    """
    return _format_voice_status("TTS", run_agentwire_cmd(["tts", "status"]))


@mcp.tool()
def stt_status() -> str:
    """Can I hear (transcribe) right now, and through what path?

    Reports the ACTIVE STT tier (default → Moonshine :8101 shim or browser
    fallback; cloud → API key; custom → shim) — only probing a server when the
    tier actually has one.

    Returns:
        Active STT path, readiness, and any warnings.
    """
    return _format_voice_status("STT", run_agentwire_cmd(["stt", "status"]))


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
