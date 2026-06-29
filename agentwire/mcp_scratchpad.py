"""MCP tools — scratchpad domain."""

from .mcp_core import (
    mcp,
    run_agentwire_cmd,
)


@mcp.tool()
def scratchpad_add(text: str, source: str = "") -> str:
    """Add a note to the user's scratch pad (the portal's slide-in notes drawer).

    Use when the user asks to save/note/remember a snippet, finding, or piece
    of text — it appears instantly in their portal drawer on every device.

    Args:
        text: Note body (plain text).
        source: Optional provenance label, e.g. your session name.

    Returns:
        Confirmation with the new note id.
    """
    args = ["scratchpad", "add", text]
    if source:
        args += ["--source", source]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to add note: {data.get('error', 'Unknown error')}"
    note = data.get("note", {})
    return f"Added scratch pad note {note.get('id', '?')}."


@mcp.tool()
def scratchpad_list() -> str:
    """List the user's scratch pad notes (newest first).

    Returns:
        Each note's id, source, and text.
    """
    data = run_agentwire_cmd(["scratchpad", "list"])
    if not data.get("success"):
        return f"Failed to list notes: {data.get('error', 'Unknown error')}"
    notes = data.get("notes", [])
    if not notes:
        return "Scratch pad is empty."
    lines = []
    for n in notes:
        src = f" [{n['source']}]" if n.get("source") else ""
        lines.append(f"- {n['id']}{src}: {n['text']}")
    return "\n".join(lines)
