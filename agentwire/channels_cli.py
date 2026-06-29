"""CLI for local speech + channel listing — ``agentwire say`` / ``agentwire channels``.

``say`` is the smart-routing voice path: if the portal has browser connections
for the session the audio plays there; otherwise it's generated and played
locally (in-process Kokoro, the custom shim, or the OS voice). The local
playback + session-inference helpers are say-private and travel with it.
TTS-server restart (used by the venv-mismatch retry) lives in ``tts_cli``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import pane_manager
from .core import (
    _get_portal_url,
    _output_json,
    _output_result,
    _portal_auth_headers,
    _post_desktop_notification,
    load_config,
)
from .project_config import get_voice_from_config


def _get_current_tmux_session() -> str | None:
    """Get the current tmux session name, if running inside tmux."""
    # Check if we're in tmux
    if not os.environ.get("TMUX"):
        return None

    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass

    return None


def _infer_session_from_path() -> str | None:
    """Infer session name from current working directory.

    ~/projects/myapp -> myapp
    ~/projects/myapp-worktrees/feature -> myapp/feature
    """
    cwd = Path.cwd()
    projects_dir = Path.home() / "projects"

    try:
        rel = cwd.relative_to(projects_dir)
        parts = rel.parts

        if len(parts) == 1:
            return parts[0]
        elif len(parts) >= 2 and "-worktrees" in parts[0]:
            # myapp-worktrees/feature -> myapp/feature
            base = parts[0].replace("-worktrees", "")
            return f"{base}/{parts[1]}"
        elif len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    except ValueError:
        pass

    return None


def _check_portal_connections(session: str, portal_url: str) -> tuple[bool, str, int]:
    """Check if portal has active browser connections for a session.

    Tries session name variants: as-is, with hostname.

    Returns:
        Tuple of (has_connections, actual_session_name, connection_count)
        - has_connections: True if there are connections (audio should go to portal)
        - actual_session_name: The session name that has connections (may include @machine)
        - connection_count: number of connected browser clients for that session
    """
    import ssl

    # Try session variants: as-is, with hostname, with @local
    session_variants = [session]
    if "@" not in session:
        hostname = socket.gethostname().split('.')[0]
        session_variants.append(f"{session}@{hostname}")
        session_variants.append(f"{session}@local")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for session_name in session_variants:
        try:
            req = urllib.request.Request(
                f"{portal_url}/api/sessions/{session_name}/connections",
                headers={"Accept": "application/json", **_portal_auth_headers()},
            )

            with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
                result = json.loads(response.read().decode())
                if result.get("has_connections", False):
                    return True, session_name, int(result.get("connection_count", 0))

        except Exception:
            continue

    # No connections found in any variant
    return False, session, 0


def _local_say_os(text: str) -> int:
    """Speak via the OS voice (default tier, no browser connected).

    macOS `say` / Linux `espeak`. Zero setup, robotic, always available.
    Absolute path on macOS — users commonly shadow `say` in PATH with an
    `agentwire say` wrapper, which would recurse into a fork bomb.
    """
    from .utils.speech import strip_speech_tags

    binary = "/usr/bin/say" if sys.platform == "darwin" else "espeak"
    try:
        subprocess.run([binary, strip_speech_tags(text)], check=True)
        return 0
    except FileNotFoundError:
        print(f"OS voice binary '{binary}' not found", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"OS voice failed: {e}", file=sys.stderr)
        return 1


def _play_wav_bytes(audio_data: bytes) -> bool:
    """Write WAV bytes to a temp file and play through system audio
    (afplay / aplay / paplay / play)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_data)
        temp_path = f.name
    try:
        players = ["afplay"] if sys.platform == "darwin" else ["aplay", "paplay", "play"]
        for player in players:
            try:
                subprocess.run([player, temp_path], check=True)
                return True
            except FileNotFoundError:
                continue
        print(f"No audio player found (tried {', '.join(players)})", file=sys.stderr)
        return False
    finally:
        Path(temp_path).unlink(missing_ok=True)


_kokoro_cli_engine = None


def _local_say_kokoro(text: str, voice: str | None) -> int:
    """Speak via in-process Kokoro (default tier, no portal running).

    Only runs when the model files are already cached — a CLI `say` never
    triggers the ~200 MB download (`agentwire tts warm` or the portal does
    that). Non-zero return → caller falls back to the OS voice.

    The engine is cached at module level: cmd_say dispatches once per text
    chunk and the model must not reload each time.
    """
    global _kokoro_cli_engine
    try:
        from .tts.local import kokoro_importable

        if not kokoro_importable():
            return 1

        from .tts.engines.kokoro import KokoroEngine, resolve_voice_name

        if not KokoroEngine.model_files_cached():
            return 1
        if _kokoro_cli_engine is None:
            _kokoro_cli_engine = KokoroEngine()

        from .tts.audio import pcm_float_to_wav_bytes
        from .tts.base import TTSRequest
        from .utils.speech import strip_speech_tags

        request = TTSRequest(
            text=strip_speech_tags(text), voice=resolve_voice_name(voice)
        )
        result = _kokoro_cli_engine.generate(request)
        wav = pcm_float_to_wav_bytes(result.audio, result.sample_rate)
        return 0 if _play_wav_bytes(wav) else 1
    except Exception as e:
        print(f"Kokoro synthesis failed ({e}); falling back to OS voice", file=sys.stderr)
        return 1


def _local_say_dispatch(
    text: str,
    voice: str,
    exaggeration: float,
    cfg_weight: float,
    tts_config: dict,
    backend: str | None = None,
    instructions: str | None = None,
    language: str = "English",
    stream: bool = False,
) -> tuple[int, str]:
    """Local (non-portal) TTS playback, dispatched on the configured tier.

    default → in-process Kokoro (OS voice until the model is cached);
    custom → HTTP shim + afplay/aplay; anything else (none) → OS voice.

    Returns (return_code, sink) where sink names the path that actually played
    ("custom-server", "local-speakers (kokoro)", "os-voice") so callers can
    report a truthful "did it play" ack instead of a blind "queued" (#444).
    """
    tier = tts_config.get("backend", "default")

    if tier == "custom":
        from .network import NetworkContext
        ctx = NetworkContext.from_config()
        tts_url = ctx.get_service_url("tts", use_tunnel=True)
        rc = _local_say(
            text, voice, exaggeration, cfg_weight, tts_url,
            backend=backend, instructions=instructions, language=language, stream=stream
        )
        return rc, "custom-server"

    if tier == "default" and _local_say_kokoro(text, voice) == 0:
        return 0, "local-speakers (kokoro)"
    return _local_say_os(text), "os-voice"


def cmd_say(args) -> int:
    """Generate TTS audio and play it.

    Smart routing:
    1. Determine session (--session flag, .agentwire.yml, path inference, or tmux)
    2. Check if portal has browser connections for that session
    3. If connections exist → send to portal (plays on browser/tablet)
    4. If no connections → generate locally and play via system audio

    Voice notification:
    - If in a worker pane (pane > 0), auto-notifies pane 0 (orchestrator)
    - Use --notify SESSION to also notify a parent session
    - Use --no-auto-notify to disable worker->orchestrator notification
    """
    text = " ".join(args.text) if args.text else ""
    json_mode = getattr(args, 'json', False)

    if not text:
        return _output_result(False, json_mode, "Usage: agentwire say <text>")

    config = load_config()
    tts_config = config.get("tts", {})
    # Voice priority: CLI flag > .agentwire.yml > global config default
    voice = args.voice or get_voice_from_config() or tts_config.get("default_voice", "default")
    exaggeration = args.exaggeration if args.exaggeration is not None else tts_config.get("exaggeration", 0.5)
    cfg_weight = args.cfg if args.cfg is not None else tts_config.get("cfg_weight", 0.5)

    # New parameters for modular TTS
    backend = getattr(args, 'backend', None)
    instructions = getattr(args, 'instructions', None)
    language = getattr(args, 'language', "English")
    stream = getattr(args, 'stream', False)

    # Determine session name (priority: flag > tmux session > path inference)
    # Tmux session is more accurate than path for forked/named sessions like "anna-fork-1"
    session = args.session or _get_current_tmux_session() or _infer_session_from_path()

    # Handle voice notifications
    _handle_voice_notifications(text, voice, args, session)

    # Asymmetric brief: if --display is given, show the human a text card toast
    # alongside the spoken audio (different content per channel). Best-effort —
    # only lands if the portal's up; the spoken path proceeds regardless.
    display = getattr(args, 'display', None)
    # Capture whether the toast actually reached the portal, so the caller can
    # report it honestly rather than claiming "shown" when the portal is down.
    toast_ok = _post_desktop_notification(display, session=session, priority="high") if display else None

    def _say_result(rc: int, sink: str, clients: int = 0) -> int:
        """Report which sink actually received the audio (#444): browser
        (played by N connected clients), local speakers / OS voice, or a
        failed dispatch — instead of a blind "queued"."""
        if json_mode:
            _output_json({"success": rc == 0, "sink": sink if rc == 0 else None,
                          "clients": clients, "session": session, "toast": toast_ok,
                          "error": None if rc == 0 else f"playback failed via {sink}"})
        return rc

    # Try portal first if we have a session
    # Portal handles chunking internally (sequential generation + broadcast)
    if session:
        portal_url = _get_portal_url()
        has_connections, actual_session, clients = _check_portal_connections(session, portal_url)

        if has_connections:
            rc = _remote_say(text, actual_session, portal_url)
            return _say_result(rc, "browser", clients)

    # No portal connections — chunk locally for better TTS quality
    from .utils.chunker import chunk_text
    chunks = chunk_text(text)

    last_sink = "os-voice"
    for chunk in chunks:
        result, last_sink = _local_say_dispatch(
            chunk, voice, exaggeration, cfg_weight, tts_config,
            backend=backend, instructions=instructions, language=language, stream=stream
        )
        if result != 0:
            return _say_result(result, last_sink)

    return _say_result(0, last_sink)


def _handle_voice_notifications(text: str, voice: str, args, session: str | None) -> None:
    """Handle voice notification to parent orchestrators.

    Auto-notify rules:
    - If in worker pane (pane > 0), notify pane 0 (local orchestrator)
    - If --notify SESSION specified, notify that session

    Args:
        text: The spoken text
        voice: Voice being used
        args: Command args (for --notify and --no-auto-notify flags)
        session: Current session name
    """
    notify_session = getattr(args, 'notify', None)
    no_auto_notify = getattr(args, 'no_auto_notify', False)

    # Get current pane index
    current_pane = pane_manager.get_current_pane_index()
    current_session = pane_manager.get_current_session()

    # Auto-notify pane 0 if we're in a worker pane (pane > 0)
    if not no_auto_notify and current_pane is not None and current_pane > 0 and current_session:
        notification = f"[VOICE] {voice} (pane {current_pane}): \"{text}\""
        try:
            pane_manager.send_to_pane(current_session, 0, notification)
        except Exception:
            pass  # Don't fail the say command if notification fails

    # Explicit --notify to another session
    if notify_session and notify_session != current_session:
        # Format: [VOICE from session] voice: "text"
        source = current_session or "unknown"
        notification = f"[VOICE from {source}] {voice}: \"{text}\""
        try:
            pane_manager.send_to_pane(notify_session, 0, notification)
        except Exception:
            pass  # Don't fail the say command if notification fails


def _local_say(
    text: str,
    voice: str,
    exaggeration: float,
    cfg_weight: float,
    tts_url: str,
    backend: str | None = None,
    instructions: str | None = None,
    language: str = "English",
    stream: bool = False,
    _retry: bool = False,
) -> int:
    """Generate TTS via the custom shim and play via system audio.

    Sends the shim contract envelope: text/voice core + opaque
    instructions/options (knobs and engine selection ride in options).
    """

    try:
        # Build contract-envelope payload
        options: dict = {
            "exaggeration": exaggeration,
            "cfg_weight": cfg_weight,
            "language": language,
            "stream": stream,
            **load_config().get("tts", {}).get("options", {}),
        }
        if backend:
            options["backend"] = backend
        payload: dict = {"text": text, "voice": voice, "options": options}
        if instructions:
            payload["instructions"] = instructions

        data = json.dumps(payload).encode()

        req = urllib.request.Request(
            f"{tts_url}/tts",
            data=data,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=60) as response:
            audio_data = response.read()

        _play_wav_bytes(audio_data)
        return 0

    except urllib.error.HTTPError as e:
        # Try to read the actual error message from the response body
        try:
            error_body = json.loads(e.read().decode())
        except Exception:
            error_body = None

        # Check for venv_mismatch error (422) - auto-restart TTS with correct venv
        if e.code == 422 and not _retry and error_body:
            if error_body.get("error") == "venv_mismatch":
                required_venv = error_body.get("required_venv")
                target_backend = error_body.get("backend", backend)
                print(f"Backend '{target_backend}' requires venv '{required_venv}'. Restarting TTS server...")

                from . import tts_cli

                if tts_cli._restart_tts_for_venv(required_venv, target_backend):
                    print("TTS server restarted. Retrying...")
                    return _local_say(
                        text, voice, exaggeration, cfg_weight, tts_url,
                        backend=target_backend, instructions=instructions, language=language,
                        stream=stream, _retry=True
                    )
                else:
                    print("Failed to restart TTS server.", file=sys.stderr)
                    return 1

        # Show the actual error message from the TTS server if available
        if error_body:
            detail = error_body.get("detail") or error_body.get("error") or error_body
            print(f"TTS error: {detail}", file=sys.stderr)
        else:
            print(f"TTS request failed: {e}", file=sys.stderr)
        return 1

    except urllib.error.URLError as e:
        print(f"TTS server not reachable: {e}", file=sys.stderr)
        print("Start it with: agentwire tts start", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"TTS failed: {e}", file=sys.stderr)
        return 1


def _remote_say(text: str, session: str, portal_url: str) -> int:
    """Send TTS to a session via the portal (for remote sessions)."""
    import ssl

    try:
        # Create SSL context that doesn't verify (self-signed certs)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        data = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            f"{portal_url}/api/say/{session}",
            data=data,
            headers={"Content-Type": "application/json", **_portal_auth_headers()},
        )

        # 90 second timeout to handle TTS cold starts
        with urllib.request.urlopen(req, context=ctx, timeout=90) as response:
            result = json.loads(response.read().decode())
            if result.get("error"):
                print(f"Error: {result['error']}", file=sys.stderr)
                return 1

        return 0

    except Exception as e:
        print(f"Failed to send to portal: {e}", file=sys.stderr)
        return 1


def cmd_channels_list(args) -> int:
    """List all registered communication channels."""
    from agentwire.channels import ChannelRegistry
    from agentwire.config import get_config

    config = get_config()
    channels = []
    for name, cls in sorted(ChannelRegistry._channels.items()):
        ch_config = config.channels.get(name)
        # Check if channel has meaningful config (api key, token, or url)
        configured = False
        if ch_config is not None:
            for attr in ("api_key", "bot_token", "account_sid", "url"):
                if getattr(ch_config, attr, ""):
                    configured = True
                    break
        # Built-in channels live under agentwire.channels.*; anything else is external.
        builtin = cls.__module__.startswith("agentwire.channels.")
        channels.append({
            "name": name,
            "type": cls.channel_type,
            "configured": configured,
            "builtin": builtin,
        })

    json_mode = getattr(args, "json", False)
    if json_mode:
        _output_json({"success": True, "channels": channels})
    else:
        if not channels:
            print("No channels registered.")
        else:
            for ch in channels:
                status = "configured" if ch["configured"] else "not configured"
                builtin = "" if ch["builtin"] else " (custom)"
                print(f"  {ch['name']:12s} {ch['type']:10s} {status}{builtin}")
    return 0


def register_channels_parser(subparsers) -> None:
    # === say command ===
    say_parser = subparsers.add_parser("say", help="Speak text via TTS")
    say_parser.add_argument("text", nargs="*", help="Text to speak")
    say_parser.add_argument("-v", "--voice", type=str, help="Voice name")
    say_parser.add_argument("-s", "--session", type=str, help="Session name (auto-detected from .agentwire.yml or tmux)")
    say_parser.add_argument("--exaggeration", type=float, help="Voice exaggeration (0-1, Chatterbox)")
    say_parser.add_argument("--cfg", type=float, help="CFG weight (0-1, Chatterbox)")
    say_parser.add_argument("--backend", type=str, help="TTS backend (chatterbox, zonos-hybrid, zonos-transformer, kokoro)")
    say_parser.add_argument("--instructions", type=str, help="Free-text style instructions passed to the TTS shim (e.g. 'speak warmly')")
    say_parser.add_argument("--language", type=str, default="English", help="Language (default: English)")
    say_parser.add_argument("--stream", action="store_true", help="Use streaming mode (if backend supports)")
    say_parser.add_argument("--notify", type=str, metavar="SESSION", help="Also notify this session (sends message as input)")
    say_parser.add_argument("--no-auto-notify", action="store_true", help="Disable auto-notify to pane 0 when in worker pane")
    say_parser.add_argument("--display", type=str, metavar="TEXT", help="Also show the human a desktop toast with this (different) text — the asymmetric brief in one call")
    say_parser.add_argument("--json", action="store_true", help="Output the sink ack as JSON (which path played: browser/local/os)")
    say_parser.set_defaults(func=cmd_say)

    # === channels command ===
    channels_parser = subparsers.add_parser("channels", help="Manage communication channels")
    channels_sub = channels_parser.add_subparsers(dest="channels_cmd")
    channels_list_parser = channels_sub.add_parser("list", help="List all registered channels")
    channels_list_parser.add_argument("--json", action="store_true", help="Output JSON")
    channels_list_parser.set_defaults(func=cmd_channels_list)
    channels_parser.set_defaults(func=cmd_channels_list)
