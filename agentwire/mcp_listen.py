"""MCP tools — listen domain."""

from .mcp_core import (
    mcp,
    run_agentwire_cmd,
)


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
def listen_cancel() -> str:
    """Cancel the current voice recording without transcribing.

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["listen", "cancel"], json_output=False)
    if data.get("success"):
        return "Recording cancelled."
    return f"Failed to cancel recording: {data.get('error', 'Unknown error')}"
