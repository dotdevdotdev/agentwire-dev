"""Voice input: record, transcribe, send to session."""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from agentwire.agents.tmux import tmux_session_exists
from agentwire.utils import config_path, load_yaml


def _load_executables_config() -> dict:
    """Load executables config from ~/.agentwire/config.yaml."""
    config = load_yaml(config_path(), default={})
    return config.get("executables", {})


def _find_executable(name: str, fallback_paths: list[str] | None = None) -> str:
    """Find executable in config, PATH, or fallback locations.

    Args:
        name: Executable name (e.g., 'ffmpeg')
        fallback_paths: List of full paths to try if not in PATH

    Returns:
        Path to executable, or the name itself if not found (will fail at runtime)
    """
    # Check config first (executables.ffmpeg, executables.whisperkit, etc.)
    exe_config = _load_executables_config()
    if name in exe_config:
        configured_path = Path(exe_config[name]).expanduser()
        if configured_path.exists():
            return str(configured_path)

    # Try PATH
    path = shutil.which(name)
    if path:
        return path

    # Try fallback paths (for restricted environments like Hammerspoon)
    if fallback_paths:
        for fallback in fallback_paths:
            if Path(fallback).exists():
                return fallback

    # Return name and let it fail at runtime with a clear error
    return name


# Find executables - check config, then PATH, then common locations for Hammerspoon
FFMPEG_PATH = _find_executable("ffmpeg", ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"])
WHISPERKIT_PATH = _find_executable("whisperkit-cli", ["/opt/homebrew/bin/whisperkit-cli"])
HS_PATH = _find_executable("hs", ["/opt/homebrew/bin/hs", "/usr/local/bin/hs"])
AGENTWIRE_PATH = _find_executable("agentwire", [
    str(Path.home() / ".local" / "bin" / "agentwire"),
    "/usr/local/bin/agentwire",
])
LOCK_FILE = Path("/tmp/agentwire-listen.lock")
PID_FILE = Path("/tmp/agentwire-listen.pid")
AUDIO_FILE = Path("/tmp/agentwire-listen.wav")
DEBUG_LOG = Path("/tmp/agentwire-listen.log")


def log(msg: str) -> None:
    """Log debug message."""
    with open(DEBUG_LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}: {msg}\n")


def notify(msg: str) -> None:
    """Show system notification (non-blocking)."""
    if sys.platform == "darwin":
        subprocess.Popen([
            "osascript", "-e",
            f'display notification "{msg}" with title "AgentWire"'
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def beep(sound: str) -> None:
    """Play system sound (non-blocking)."""
    if sys.platform == "darwin":
        sounds = {
            "start": "/System/Library/Sounds/Blow.aiff",
            "stop": "/System/Library/Sounds/Pop.aiff",
            "done": "/System/Library/Sounds/Glass.aiff",
            "error": "/System/Library/Sounds/Basso.aiff",
        }
        if sound in sounds:
            subprocess.Popen(["afplay", sounds[sound]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load_config() -> dict:
    """Load agentwire config."""
    return load_yaml(config_path(), default={})


def transcribe_via_server(audio_path: Path, stt_url: str, timeout: int = 30) -> str | None:
    """Try to transcribe via STT server.

    Returns transcribed text on success, None if server unavailable.
    """
    import urllib.request
    import urllib.error
    import json

    try:
        # Check if server is healthy first (fast fail)
        health_req = urllib.request.Request(f"{stt_url}/health")
        with urllib.request.urlopen(health_req, timeout=2) as resp:
            health = json.loads(resp.read().decode())
            if health.get("status") != "ok":
                return None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    # Server is up, send audio for transcription
    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()

        # Build multipart form data
        boundary = "----AgentWireBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + audio_data + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"{stt_url}/transcribe",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
            text = result.get("text", "").strip()
            log(f"STT server transcribed in {result.get('transcribe_time', '?')}s")
            return text if text else None

    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        log(f"STT server error: {e}")
        return None


def get_audio_device() -> str:
    """Get audio input device from config. Returns device index for ffmpeg."""
    config = load_config()
    # audio.input_device can be an integer index or "default"
    device = config.get("audio", {}).get("input_device", "default")
    if device == "default":
        return "default"
    return str(device)


def start_recording() -> int:
    """Start recording audio."""
    log("start_recording called")

    # Clean up any stale recording
    subprocess.run(["pkill", "-9", "-f", "ffmpeg.*agentwire-listen\\.wav"],
                   capture_output=True)
    LOCK_FILE.unlink(missing_ok=True)
    PID_FILE.unlink(missing_ok=True)
    AUDIO_FILE.unlink(missing_ok=True)
    time.sleep(0.1)

    LOCK_FILE.touch()
    beep("start")

    # Record audio (16kHz mono for whisper)
    device = get_audio_device()

    if sys.platform == "darwin":
        # Build input specifier: ":N" for specific device, or ":default"
        if device == "default":
            input_spec = ":default"
        else:
            input_spec = f":{device}"

        proc = subprocess.Popen(
            [FFMPEG_PATH, "-f", "avfoundation", "-i", input_spec,
             "-ar", "16000", "-ac", "1",
             "-acodec", "pcm_s16le",  # Uncompressed for quality
             str(AUDIO_FILE), "-y"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        # Linux - use pulse or alsa
        proc = subprocess.Popen(
            ["ffmpeg", "-f", "pulse", "-i", "default",
             "-ar", "16000", "-ac", "1",
             "-acodec", "pcm_s16le",
             str(AUDIO_FILE), "-y"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    PID_FILE.write_text(str(proc.pid))
    log(f"Started ffmpeg with PID {proc.pid}")
    print("Recording...")
    return 0


def stop_recording(session: str, voice_prompt: bool = True, type_at_cursor: bool = False) -> int:
    """Stop recording, transcribe, and send to session or type at cursor.

    Args:
        session: Target tmux session (ignored if type_at_cursor=True)
        voice_prompt: Prepend voice prompt hint (ignored if type_at_cursor=True)
        type_at_cursor: If True, type text at cursor instead of sending to session
    """
    log("stop_recording called")

    if not LOCK_FILE.exists():
        log("ERROR: No lock file")
        print("Not recording")
        beep("error")
        return 1

    beep("stop")
    log("Stopping ffmpeg")

    # Stop ffmpeg gracefully
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            # Give ffmpeg time to flush and exit gracefully
            time.sleep(0.3)
        except (ValueError, ProcessLookupError):
            pass
        PID_FILE.unlink(missing_ok=True)

    # Force kill any remaining ffmpeg processes
    subprocess.run(["pkill", "-9", "-f", "ffmpeg.*agentwire-listen\\.wav"],
                   capture_output=True)
    LOCK_FILE.unlink(missing_ok=True)

    # Wait for file to be fully written
    time.sleep(0.3)

    # Verify file exists and has content
    if not AUDIO_FILE.exists():
        log("ERROR: No audio file")
        notify("Recording failed")
        beep("error")
        return 1

    # Wait for file to stabilize (size stops changing)
    last_size = 0
    for _ in range(10):  # Max 1 second wait
        current_size = AUDIO_FILE.stat().st_size
        if current_size > 0 and current_size == last_size:
            break
        last_size = current_size
        time.sleep(0.1)

    if AUDIO_FILE.stat().st_size < 1000:  # Less than 1KB is likely corrupt
        log(f"ERROR: Audio file too small ({AUDIO_FILE.stat().st_size} bytes)")
        notify("Recording too short")
        beep("error")
        return 1

    log("Transcribing...")
    notify("Transcribing...")

    # Get config
    config = load_config()
    stt_config = config.get("stt", {})
    stt_url = stt_config.get("url", "http://localhost:8101")

    text = ""

    # Try STT server first (instant if running)
    text = transcribe_via_server(AUDIO_FILE, stt_url)

    if text:
        log(f"Used STT server at {stt_url}")
    else:
        # Fall back to whisperkit-cli (slower cold start)
        log("STT server unavailable, using whisperkit-cli...")
        model_path = stt_config.get("model_path") or os.path.expanduser(
            "~/Library/Application Support/MacWhisper/models/whisperkit/models/"
            "argmaxinc/whisperkit-coreml/openai_whisper-large-v3-v20240930"
        )

        try:
            result = subprocess.run(
                [
                    WHISPERKIT_PATH, "transcribe",
                    "--audio-path", str(AUDIO_FILE),
                    "--model-path", model_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                log(f"ERROR: whisperkit-cli failed: {result.stderr}")
                notify("Transcription failed")
                beep("error")
                return 1

            text = result.stdout.strip()

        except subprocess.TimeoutExpired:
            log("ERROR: whisperkit-cli timed out")
            notify("Transcription timed out")
            beep("error")
            return 1
        except Exception as e:
            log(f"ERROR: STT failed: {e}")
            notify(f"Transcription failed: {e}")
            beep("error")
            return 1

    if not text:
        log("ERROR: No speech detected")
        notify("No speech detected")
        beep("error")
        AUDIO_FILE.unlink(missing_ok=True)
        return 1

    log(f"Transcribed: {text}")

    if type_at_cursor:
        # Type at cursor using Hammerspoon
        log("Typing at cursor...")

        # Escape text for Lua string (handle quotes and backslashes)
        escaped_text = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

        # Use Hammerspoon to paste from clipboard, wait, press Enter, restore clipboard
        hs_script = f'''
            local original = hs.pasteboard.getContents()
            hs.pasteboard.setContents("{escaped_text}")
            hs.eventtap.keyStroke({{"cmd"}}, "v")
            hs.timer.usleep(1000000)
            hs.eventtap.keyStroke({{}}, "return")
            hs.timer.usleep(100000)
            if original then
                hs.pasteboard.setContents(original)
            else
                hs.pasteboard.clearContents()
            end
        '''

        result = subprocess.run(
            [HS_PATH, "-c", hs_script],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            log(f"ERROR: Hammerspoon failed: {result.stderr}")
            notify("Failed to type text")
            beep("error")
            AUDIO_FILE.unlink(missing_ok=True)
            return 1

        beep("done")
        log("SUCCESS: Typed at cursor")
        notify(f"Typed: {text[:30]}...")
        print(f"Typed: {text}")
    else:
        # Send to tmux session (original behavior)
        if not tmux_session_exists(session):
            log(f"ERROR: No session '{session}'")
            notify(f"No session: {session}")
            beep("error")
            print(f"Transcribed: {text}")
            print(f"But session '{session}' not running. Start with: agentwire dev")
            AUDIO_FILE.unlink(missing_ok=True)
            return 1

        # Build message
        if voice_prompt:
            full_text = f"[User said: '{text}' - respond using MCP tool: agentwire_say(text=\"your message\")]"
        else:
            full_text = text

        log(f"Sending to session: {session}")

        # Use agentwire send CLI for consistent behavior
        result = subprocess.run(
            [AGENTWIRE_PATH, "send", "-s", session, full_text],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            log(f"ERROR: agentwire send failed: {result.stderr}")
            notify("Failed to send to session")
            beep("error")
            AUDIO_FILE.unlink(missing_ok=True)
            return 1

        beep("done")
        log("SUCCESS: Sent to session")
        notify(f"Sent: {text[:30]}...")
        print(f"Sent to {session}: {text}")

    AUDIO_FILE.unlink(missing_ok=True)
    return 0


def cancel_recording() -> int:
    """Cancel current recording."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
        PID_FILE.unlink(missing_ok=True)

    subprocess.run(["pkill", "-9", "-f", "ffmpeg.*agentwire-listen\\.wav"],
                   capture_output=True)
    LOCK_FILE.unlink(missing_ok=True)
    AUDIO_FILE.unlink(missing_ok=True)

    beep("error")
    notify("Cancelled")
    print("Cancelled")
    return 0


def is_recording() -> bool:
    """Check if currently recording."""
    return LOCK_FILE.exists()
