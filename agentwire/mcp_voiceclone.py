"""MCP tools — voiceclone domain."""

from .mcp_core import (
    format_voices,
    mcp,
    run_agentwire_cmd,
)


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
