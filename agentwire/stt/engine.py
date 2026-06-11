"""Backend loading and transcription for the STT server.

FastAPI-free so backend selection stays unit-testable without the
``[stt]`` extras installed. ``stt_server.py`` is the HTTP wrapper
around this module.

Backends, in ``auto`` fallback order: ``moonshine`` (Moonshine ONNX,
fast CPU inference), then ``whisper`` (faster-whisper, then
openai-whisper). Hosted transcription APIs are not a shim concern —
that's the portal's ``stt.backend: cloud`` tier (``stt/cloud.py``).
"""

import os
import tempfile
import time

KNOWN_BACKENDS = ("auto", "moonshine", "whisper")


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


def load_backend(
    backend: str = "auto",
    whisper_model: str = "base",
    whisper_device: str = "cpu",
    moonshine_model: str = "moonshine/base",
) -> tuple[object, dict]:
    """Load an STT backend, returning ``(model, model_info)``."""
    if backend not in KNOWN_BACKENDS:
        print(f"Unknown STT_BACKEND '{backend}', falling back to auto")
        backend = "auto"

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

    raise RuntimeError(
        "No STT backend available. Install useful-moonshine-onnx, faster-whisper, "
        "or openai-whisper."
    )


def transcribe(model: object, model_info: dict, audio_path: str) -> dict:
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
