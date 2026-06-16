"""Voice cloning: record audio and upload to TTS server."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

from agentwire.utils import config_path, load_yaml

LOCK_FILE = Path("/tmp/agentwire-voiceclone.lock")
PID_FILE = Path("/tmp/agentwire-voiceclone.pid")
AUDIO_FILE = Path("/tmp/agentwire-voiceclone.wav")
DEBUG_LOG = Path("/tmp/agentwire-voiceclone.log")


def log(msg: str) -> None:
    """Log debug message."""
    with open(DEBUG_LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}: {msg}\n")


def notify(msg: str) -> None:
    """Show system notification (non-blocking)."""
    if sys.platform == "darwin":
        subprocess.Popen([
            "osascript", "-e",
            f'display notification "{msg}" with title "AgentWire Voice Clone"'
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


def get_tts_config() -> dict:
    """Get TTS configuration."""
    config = load_config()
    return config.get("tts", {})


def get_tts_url() -> str | None:
    """Get TTS server URL from config."""
    return get_tts_config().get("url")


def is_custom_backend() -> bool:
    """True iff a custom TTS shim is configured (voice cloning requires one)."""
    return get_tts_config().get("backend") == "custom"


def get_audio_device() -> str:
    """Get audio input device from config. Returns device index for ffmpeg."""
    config = load_config()
    # audio.input_device can be an integer index or "default"
    device = config.get("audio", {}).get("input_device", "default")
    if device == "default":
        return "default"
    return str(device)


def start_recording() -> int:
    """Start recording audio for voice cloning."""
    log("start_recording called")

    # Voice cloning requires a custom TTS shim — fail before the user talks for 10s.
    if not is_custom_backend():
        beep("error")
        log("ERROR: voice cloning requires tts.backend: custom")
        notify("Voice cloning requires a custom TTS shim")
        print("Error: voice cloning requires tts.backend: custom with a shim that "
              "supports POST /voices/{name}. See docs/wiki/voice/shim-contract.md.")
        return 1

    # Clean up any stale recording
    subprocess.run(["pkill", "-9", "-f", "ffmpeg.*agentwire-voiceclone"],
                   capture_output=True)
    LOCK_FILE.unlink(missing_ok=True)
    PID_FILE.unlink(missing_ok=True)
    AUDIO_FILE.unlink(missing_ok=True)
    time.sleep(0.1)

    LOCK_FILE.touch()
    beep("start")

    # Record audio at native sample rate, apply filters, resample to 24kHz before upload
    device = get_audio_device()

    if sys.platform == "darwin":
        # Build input specifier: ":N" for specific device, or use default
        if device == "default":
            input_spec = ":default"
        else:
            input_spec = f":{device}"

        # Audio filters for cleaner recording:
        # - highpass: remove low rumble below 80Hz
        # - lowpass: remove hiss above 16kHz
        # - dynaudnorm: normalize volume levels
        audio_filter = "highpass=f=80,lowpass=f=16000,dynaudnorm=p=0.9:s=5"

        proc = subprocess.Popen(
            ["ffmpeg", "-f", "avfoundation", "-i", input_spec,
             "-af", audio_filter,
             "-ar", "44100", "-ac", "1",  # Native sample rate, mono
             "-acodec", "pcm_s16le",  # Uncompressed
             str(AUDIO_FILE), "-y"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        # Linux - use pulse or alsa
        audio_filter = "highpass=f=80,lowpass=f=16000,dynaudnorm=p=0.9:s=5"
        proc = subprocess.Popen(
            ["ffmpeg", "-f", "pulse", "-i", "default",
             "-af", audio_filter,
             "-ar", "44100", "-ac", "1",
             "-acodec", "pcm_s16le",
             str(AUDIO_FILE), "-y"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    PID_FILE.write_text(str(proc.pid))
    log(f"Started ffmpeg with PID {proc.pid}")
    print("Recording for voice clone... (10-30 seconds recommended)")
    return 0


def stop_recording(voice_name: str) -> int:
    """Stop recording and upload voice clone."""
    log(f"stop_recording called for voice: {voice_name}")

    if not LOCK_FILE.exists():
        log("ERROR: No lock file")
        print("Not recording")
        beep("error")
        return 1

    beep("stop")
    log("Stopping ffmpeg")

    # Stop ffmpeg
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
        PID_FILE.unlink(missing_ok=True)

    subprocess.run(["pkill", "-9", "-f", "ffmpeg.*agentwire-voiceclone"],
                   capture_output=True)
    LOCK_FILE.unlink(missing_ok=True)

    # Wait for file to be written
    time.sleep(0.3)

    if not AUDIO_FILE.exists():
        log("ERROR: No audio file")
        notify("Recording failed")
        beep("error")
        return 1

    # Check audio duration
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(AUDIO_FILE)],
        capture_output=True, text=True
    )
    try:
        duration = float(result.stdout.strip())
        log(f"Audio duration: {duration:.1f}s")
        if duration < 3:
            log("ERROR: Recording too short")
            notify("Recording too short (min 3 seconds)")
            beep("error")
            AUDIO_FILE.unlink(missing_ok=True)
            return 1
    except (ValueError, AttributeError):
        log("WARNING: Could not determine audio duration")

    # Resample to 24kHz mono (required by Chatterbox TTS)
    log("Resampling to 24kHz...")
    resampled_file = Path("/tmp/agentwire-voiceclone-24k.wav")
    resample_result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(AUDIO_FILE),
         "-ar", "24000", "-ac", "1",
         str(resampled_file)],
        capture_output=True, text=True
    )
    if resample_result.returncode != 0:
        log(f"ERROR: Resample failed - {resample_result.stderr}")
        notify("Resample failed")
        beep("error")
        AUDIO_FILE.unlink(missing_ok=True)
        return 1

    # Use resampled file for upload
    upload_file = resampled_file

    log("Uploading voice clone...")
    notify("Uploading voice clone...")
    print(f"Uploading voice '{voice_name}'...")

    # Cleanup helper
    def cleanup():
        AUDIO_FILE.unlink(missing_ok=True)
        upload_file.unlink(missing_ok=True)

    tts_url = get_tts_url()
    try:
        with open(upload_file, "rb") as f:
            response = requests.post(
                f"{tts_url}/voices/{voice_name}",
                files={"file": (f"{voice_name}.wav", f, "audio/wav")}
            )

        if response.status_code == 200:
            data = response.json()
            duration = data.get("duration", "?")
            beep("done")
            log(f"SUCCESS: Voice '{voice_name}' created ({duration}s)")
            notify(f"Voice '{voice_name}' created!")
            print(f"Voice '{voice_name}' created successfully ({duration}s)")
            cleanup()
            return 0
        else:
            error = response.json().get("detail", response.text)
            log(f"ERROR: Upload failed - {error}")
            notify(f"Upload failed: {error}")
            beep("error")
            print(f"Upload failed: {error}")
            cleanup()
            return 1

    except requests.RequestException as e:
        log(f"ERROR: Connection failed - {e}")
        notify("Connection to TTS server failed")
        beep("error")
        print(f"Connection failed: {e}")
        cleanup()
        return 1


def cancel_recording() -> int:
    """Cancel current recording."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
        PID_FILE.unlink(missing_ok=True)

    subprocess.run(["pkill", "-9", "-f", "ffmpeg.*agentwire-voiceclone"],
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


def list_voices() -> int:
    """List available voices from the custom TTS shim."""
    if not is_custom_backend():
        print("No cloned voices in default tier (browser voice). "
              "Voice cloning requires tts.backend: custom.")
        return 0
    tts_url = get_tts_url()
    try:
        response = requests.get(f"{tts_url}/voices")
        if response.status_code == 200:
            data = response.json()
            voices = data.get("voices", data) if isinstance(data, dict) else data

            if not voices:
                print("No voices available")
                return 0

            print(f"Available voices ({len(voices)}):")
            for v in sorted(voices, key=lambda x: x.get("name", "")):
                name = v.get("name", "?")
                duration = v.get("duration", "?")
                print(f"  {name}: {duration}s")
            return 0
        else:
            print(f"Failed to list voices: {response.status_code}")
            return 1
    except requests.RequestException as e:
        print(f"Connection failed: {e}")
        return 1


def delete_voice(voice_name: str) -> int:
    """Delete a voice from TTS server."""
    tts_url = get_tts_url()
    if not tts_url:
        print("Error: tts.url not configured in config.yaml")
        return 1
    try:
        response = requests.delete(f"{tts_url}/voices/{voice_name}")
        if response.status_code == 200:
            print(f"Voice '{voice_name}' deleted")
            return 0
        elif response.status_code == 404:
            print(f"Voice '{voice_name}' not found")
            return 1
        else:
            error = response.json().get("detail", response.text)
            print(f"Delete failed: {error}")
            return 1
    except requests.RequestException as e:
        print(f"Connection failed: {e}")
        return 1
