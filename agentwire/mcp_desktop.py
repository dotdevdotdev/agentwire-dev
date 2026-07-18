"""MCP tools — desktop domain."""

from .mcp_core import (
    get_portal_url,
    mcp,
)


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

    from .core import portal_request

    url = f"{get_portal_url()}{path}"
    try:
        if method in ("GET", "DELETE"):
            resp = portal_request(method, url)
        else:
            resp = portal_request(method, url, json=body or {})

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


def _announce_artifact(url: str, title: str, artifact_id: str | None) -> dict:
    """POST the click-to-open artifact notification (#817) — the single
    portal-side path for surfacing an artifact; there is no force-open."""
    artifact = {"url": url, "title": title}
    if artifact_id:
        artifact["artifact_id"] = artifact_id
    return _portal_request("POST", "/api/desktop/notification", {"artifact": artifact})


@mcp.tool()
def desktop_open_artifact(url: str, title: str = "Artifact", artifact_id: str | None = None) -> str:
    """Announce a URL or local artifact file on the portal desktop (click-to-open).

    Posts a notification (toast + Session HUD entry) carrying the artifact
    target instead of force-opening a window (#817) — a background-produced
    artifact must never steal focus. The human clicks the notice to open the
    artifact window, focused.

    For local files, use a filename from ~/.agentwire/artifacts/ (e.g., "dashboard.html").
    For external sites, use a full URL (e.g., "https://example.com").

    Args:
        url: URL or filename to display. Filenames are served from ~/.agentwire/artifacts/.
        title: Window title (default: "Artifact")
        artifact_id: Optional unique window ID. If omitted, derived from URL.

    Returns:
        Notification ID or error description.
    """
    data = _announce_artifact(url, title, artifact_id)
    if data.get("success"):
        return f"Artifact '{title}' announced (notification id: {data.get('id', 'unknown')}) — the human clicks to open."
    return f"Failed to announce artifact: {data.get('error', 'Unknown error')}"


@mcp.tool()
def desktop_write_artifact(
    filename: str,
    html_content: str,
    title: str = "Artifact",
    artifact_id: str | None = None,
) -> str:
    """Write HTML content to a file and announce it as a click-to-open artifact.

    Atomically writes content to ~/.agentwire/artifacts/<filename>, then posts
    a click-to-open notification (toast + Session HUD entry, #817) — the human
    clicks to open the artifact window; it never steals focus. Use this to
    deliver dashboards, diagrams, reports, or any HTML content.

    Args:
        filename: Output filename (must end in .html, e.g., "dashboard.html")
        html_content: Complete HTML content to write
        title: Window title (default: "Artifact")
        artifact_id: Optional unique window ID. If omitted, derived from filename.

    Returns:
        Notification ID or error description.
    """
    upload_data = _portal_request("POST", "/api/artifacts/upload", {
        "filename": filename,
        "content": html_content,
    })
    if not upload_data.get("success"):
        return f"Failed to write artifact: {upload_data.get('error', 'Unknown error')}"

    open_data = _announce_artifact(filename, title, artifact_id)
    if open_data.get("success"):
        return f"Artifact '{filename}' written and announced (notification id: {open_data.get('id', 'unknown')})."
    return f"File written but failed to announce it: {open_data.get('error', 'Unknown error')}"


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
def desktop_collage() -> str:
    """Toggle the window collage in the portal desktop.

    Lays every open window into a grid so they can all be seen at once;
    toggling again (or the user clicking a tile / pressing Esc) exits the overlay.

    Returns:
        Success message or error description.
    """
    data = _portal_request("POST", "/api/desktop/collage")
    if data.get("success"):
        return "Collage toggled."
    return f"Failed to toggle Collage: {data.get('error', 'Unknown error')}"


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
