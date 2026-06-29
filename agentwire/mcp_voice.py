"""MCP tools — voice domain."""

import base64

from .mcp_core import (
    format_voices,
    get_portal_url,
    mcp,
    run_agentwire_cmd,
)


def _fetch_tts_tool_prompt() -> str:
    """Fetch the shim-authored `tool_prompt` from a custom TTS shim's
    GET /capabilities (fail-soft, 1.5s).

    This is the producer end of the capability loop: the shim dev writes a
    prompt describing what their model accepts (emotion tags, style
    instructions), and we append it verbatim to the `say` tooldef so agents
    actually emit those tags. Tooldefs load at MCP-server start — a shim swap
    needs a session restart to re-teach running agents.
    """
    try:
        import json as _json
        import urllib.request

        from .config import load_config as _load_typed

        cfg = _load_typed()
        if cfg.tts.backend != "custom" or not cfg.tts.url:
            return ""
        with urllib.request.urlopen(f"{cfg.tts.url.rstrip('/')}/capabilities", timeout=1.5) as r:
            return (_json.load(r).get("tool_prompt") or "").strip()
    except Exception:
        return ""  # fail-soft: stock description


_TTS_TOOL_PROMPT = _fetch_tts_tool_prompt()


_SAY_DESCRIPTION = (
    "Speak text via TTS.\n\n"
    "Audio routes to the browser portal if connected, otherwise local speakers.\n\n"
    "Args:\n"
    "    text: Text to speak\n"
    "    session: Target session for audio routing (optional)\n"
    "    voice: Voice name to use (optional, uses default if not specified)\n\n"
    "Returns:\n"
    "    Success message or error description."
    + (f"\n\nBackend capabilities:\n{_TTS_TOOL_PROMPT}" if _TTS_TOOL_PROMPT else "")
)


@mcp.tool(description=_SAY_DESCRIPTION)
def say(text: str, session: str | None = None, voice: str | None = None, display: str | None = None) -> str:
    """Speak text via TTS — description built dynamically in _SAY_DESCRIPTION.

    `display` (optional) shows the human a desktop toast *at the same time*, with
    DIFFERENT content from the spoken text — the asymmetric brief in one call:
    `text` is the punchy spoken headline, `display` is the richer scannable card
    (supports bold/links/line breaks). Pairs voice + screen atomically so you
    don't have to remember a separate notify_user call.
    """
    # Quick TTS health check — fail fast if a custom shim is unreachable.
    # Default tier has no server dependency (browser/OS voice), nothing to probe.
    try:
        import urllib.request

        from .config import load_config as load_typed_config
        from .network import NetworkContext

        cfg = load_typed_config()
        if cfg.tts.backend == "custom":
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
    if display:
        args.extend(["--display", display])
    args.append(text)

    data = run_agentwire_cmd(args, timeout=120)
    if not data.get("success"):
        return f"Failed to speak: {data.get('error', 'Unknown error')}"

    # Sink ack (#444): report which path actually played, not a blind "queued".
    sink = data.get("sink")
    # Toast ack: report the toast's real fate, not a blind "shown". `toast` is
    # True if the portal accepted it, False if the portal was unreachable, None
    # if no display was requested.
    toast_ok = data.get("toast")
    if toast_ok is None:
        toast = ""
    elif toast_ok:
        toast = " Toast posted to the portal."
    else:
        toast = " (toast not delivered — portal unreachable)."
    if sink == "browser":
        n = data.get("clients", 0)
        return f"Spoken to {n} connected browser client{'s' if n != 1 else ''}.{toast}"
    if sink == "local-speakers (kokoro)":
        return f"Spoken on local speakers (in-process Kokoro).{toast}"
    if sink == "custom-server":
        return f"Spoken via custom TTS shim on local speakers.{toast}"
    if sink == "os-voice":
        return f"Spoken via the OS voice (no browser connected).{toast}"
    return f"Speech dispatched.{toast}"


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

    from .security import get_local_portal_token

    token = get_local_portal_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        # Create multipart form data
        files = {"audio": (f"audio.{format}", audio_bytes, mime_type)}
        response = requests.post(url, files=files, headers=headers, verify=False, timeout=60)

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
