"""Backend loading and transcription for the STT server.

FastAPI-free so backend selection and the cloud path stay unit-testable
without the ``[stt]`` extras installed. ``stt_server.py`` is the HTTP
wrapper around this module.

Backends, in ``auto`` fallback order:

- ``moonshine``      — Moonshine ONNX, fast CPU inference
- ``whisper``        — faster-whisper, then openai-whisper
- ``cloud-openai``   — OpenAI transcription API (last resort in ``auto``,
                       only when ``OPENAI_API_KEY`` is set; force with
                       ``STT_BACKEND=cloud-openai``)

The OpenAI key is read from the server process environment only. It is
never stored in ``model_info`` (which ``/health`` and ``/capabilities``
echo back) and never reaches the browser.
"""

import json
import os
import tempfile
import time
import urllib.request

KNOWN_BACKENDS = ("auto", "moonshine", "whisper", "cloud-openai")

OPENAI_DEFAULT_MODEL = "gpt-4o-mini-transcribe"
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"

_AUDIO_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/m4a",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}


def _load_moonshine(moonshine_model: str) -> tuple[object, dict]:
    """Load Moonshine ONNX and warm it up with a dummy transcription."""
    import moonshine_onnx
    import numpy as np
    import soundfile as sf

    print(f"Loading Moonshine ONNX model: {moonshine_model}...")
    start = time.time()
    dummy = np.zeros(16000, dtype=np.float32)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        sf.write(f.name, dummy, 16000)
        moonshine_onnx.transcribe(f.name, moonshine_model)
        os.unlink(f.name)
    elapsed = time.time() - start
    print(f"Moonshine ONNX loaded in {elapsed:.2f}s")
    return moonshine_onnx, {
        "backend": "moonshine",
        "model": moonshine_model,
        "load_time": round(elapsed, 2),
    }


def _load_faster_whisper(whisper_model: str, device: str) -> tuple[object, dict]:
    """Load faster-whisper."""
    from faster_whisper import WhisperModel

    compute_type = "float32" if device == "cpu" else "float16"
    print(f"Loading faster-whisper model: {whisper_model} on {device}...")
    start = time.time()
    model = WhisperModel(whisper_model, device=device, compute_type=compute_type)
    elapsed = time.time() - start
    print(f"Model loaded in {elapsed:.2f}s")
    return model, {
        "backend": "faster-whisper",
        "model": whisper_model,
        "device": device,
        "compute_type": compute_type,
        "load_time": round(elapsed, 2),
    }


def _load_openai_whisper(whisper_model: str, device: str) -> tuple[object, dict]:
    """Load openai-whisper."""
    import whisper

    print(f"Loading openai-whisper model: {whisper_model}...")
    start = time.time()
    model = whisper.load_model(whisper_model, device=device)
    elapsed = time.time() - start
    print(f"Model loaded in {elapsed:.2f}s")
    return model, {
        "backend": "openai-whisper",
        "model": whisper_model,
        "device": device,
        "load_time": round(elapsed, 2),
    }


def _init_cloud_openai() -> tuple[None, dict]:
    """Initialize the cloud-openai backend (no model to load).

    Raises RuntimeError if OPENAI_API_KEY is missing — the key itself is
    re-read from the environment per request and never stored here.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "cloud-openai backend requires OPENAI_API_KEY in the server environment"
        )
    model = os.environ.get("OPENAI_STT_MODEL", OPENAI_DEFAULT_MODEL)
    print(f"Using cloud-openai backend: {model} (no local model)")
    return None, {"backend": "cloud-openai", "model": model}


def load_backend(
    backend: str = "auto",
    whisper_model: str = "base",
    whisper_device: str = "cpu",
    moonshine_model: str = "moonshine/base",
) -> tuple[object | None, dict]:
    """Load an STT backend, returning ``(model, model_info)``.

    ``model`` is None for cloud-openai (nothing to hold in memory).
    In ``auto`` mode the local backends are tried first; cloud-openai is
    the final fallback and only when OPENAI_API_KEY is set.
    """
    if backend not in KNOWN_BACKENDS:
        print(f"Unknown STT_BACKEND '{backend}', falling back to auto")
        backend = "auto"

    if backend == "cloud-openai":
        return _init_cloud_openai()

    if backend in ("auto", "moonshine"):
        try:
            return _load_moonshine(moonshine_model)
        except ImportError:
            if backend == "moonshine":
                raise RuntimeError(
                    "useful-moonshine-onnx not installed. Run: pip install useful-moonshine-onnx soundfile"
                )
            print("moonshine_onnx not available, trying faster-whisper...")
        except Exception as e:
            if backend == "moonshine":
                raise
            print(f"Moonshine failed ({e}), trying faster-whisper...")

    if backend in ("auto", "whisper"):
        try:
            return _load_faster_whisper(whisper_model, whisper_device)
        except ImportError:
            print("faster-whisper not available, trying openai-whisper...")
        except Exception as e:
            if backend == "whisper":
                raise
            print(f"faster-whisper failed ({e}), trying openai-whisper...")

        try:
            return _load_openai_whisper(whisper_model, whisper_device)
        except ImportError:
            if backend == "whisper":
                raise RuntimeError(
                    "No Whisper backend available. Install faster-whisper or openai-whisper."
                )
            print("openai-whisper not available...")
        except Exception as e:
            if backend == "whisper":
                raise
            print(f"openai-whisper failed ({e})...")

    # auto only: cloud fallback when local models can't load
    if os.environ.get("OPENAI_API_KEY"):
        print("No local STT backend available, falling back to cloud-openai")
        return _init_cloud_openai()

    raise RuntimeError(
        "No STT backend available. Install a local backend (useful-moonshine-onnx, "
        "faster-whisper, or openai-whisper) or set OPENAI_API_KEY to use cloud-openai."
    )


def transcribe_cloud_openai(audio_path: str, model: str, timeout: float | None = None) -> dict:
    """Transcribe via the OpenAI transcription API.

    Builds the multipart request with stdlib urllib (no SDK dependency).
    The API key is read from the environment at call time, sent only in the
    Authorization header of this server-side request.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in server environment")

    base_url = os.environ.get("OPENAI_BASE_URL", OPENAI_DEFAULT_BASE_URL).rstrip("/")
    if timeout is None:
        timeout = float(os.environ.get("OPENAI_STT_TIMEOUT", "30"))

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    ext = os.path.splitext(audio_path)[1].lower() or ".wav"
    content_type = _AUDIO_CONTENT_TYPES.get(ext, "application/octet-stream")

    boundary = "----AgentWireBoundary"
    parts = [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{model}"
        ).encode(),
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
            f"json"
        ).encode(),
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="audio{ext}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + audio_data,
    ]
    body = b"\r\n".join(parts) + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{base_url}/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())

    return {
        "text": payload.get("text", "").strip(),
        "language": payload.get("language", "en"),
        "duration": payload.get("duration"),
    }


def transcribe(model: object | None, model_info: dict, audio_path: str) -> dict:
    """Transcribe an audio file with the loaded backend."""
    backend = model_info.get("backend")
    if not backend:
        raise RuntimeError("Model not loaded")

    start = time.time()

    if backend == "moonshine":
        texts = model.transcribe(audio_path, model_info["model"])
        text = " ".join(t.strip() for t in texts) if isinstance(texts, (list, tuple)) else str(texts).strip()
        result = {
            "text": text,
            "language": "en",
            "duration": None,
        }
    elif backend == "faster-whisper":
        segments, info = model.transcribe(
            audio_path,
            beam_size=5,
            language="en",
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments)
        result = {
            "text": text,
            "language": info.language,
            "duration": round(info.duration, 2),
        }
    elif backend == "cloud-openai":
        result = transcribe_cloud_openai(audio_path, model_info["model"])
    else:
        # openai-whisper
        raw = model.transcribe(audio_path, language="en")
        result = {
            "text": raw["text"].strip(),
            "language": raw.get("language", "en"),
            "duration": None,
        }

    result["transcribe_time"] = round(time.time() - start, 2)
    return result
