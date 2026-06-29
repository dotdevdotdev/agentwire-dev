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
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    from .security import get_local_portal_token

    token = get_local_portal_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    url = f"{get_portal_url()}{path}"
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, verify=False, timeout=10)
        elif method == "DELETE":
            resp = requests.delete(url, headers=headers, verify=False, timeout=10)
        else:
            resp = requests.post(url, json=body or {}, headers=headers, verify=False, timeout=10)

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
