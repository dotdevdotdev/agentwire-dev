#!/usr/bin/env python3
"""AgentWire STT Server - Persistent Whisper for fast transcription.

Keeps the Whisper model loaded in memory to eliminate cold start delays.
Designed for local use to avoid audio upload latency.

Run via:
    agentwire stt start                     # Start in tmux (CPU)
    agentwire stt start --model large-v3    # Specific model
    agentwire stt stop                      # Stop the server
    agentwire stt status                    # Check status

Or run directly:
    WHISPER_MODEL=base uvicorn agentwire.stt.stt_server:app --host 0.0.0.0 --port 8101
"""

import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

# Configuration via environment
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
STT_HOST = os.environ.get("STT_HOST", "0.0.0.0")
STT_PORT = int(os.environ.get("STT_PORT", "8101"))
# Set STT_BACKEND=moonshine to force Moonshine, STT_BACKEND=whisper to force faster-whisper
STT_BACKEND = os.environ.get("STT_BACKEND", "auto")
# Moonshine model: moonshine/tiny (fastest) or moonshine/base (better accuracy)
MOONSHINE_MODEL = os.environ.get("MOONSHINE_MODEL", "moonshine/base")

# Global model instance
whisper_model = None
model_info = {}


def load_whisper_model():
    """Load STT model based on environment config."""
    global whisper_model, model_info

    # Try Moonshine ONNX first (fast CPU inference, no GPU/torch required)
    if STT_BACKEND in ("auto", "moonshine"):
        try:
            import moonshine_onnx
            import numpy as np
            import soundfile as sf

            print(f"Loading Moonshine ONNX model: {MOONSHINE_MODEL}...")
            start = time.time()
            # Warm up: load model weights with a dummy transcription
            dummy = np.zeros(16000, dtype=np.float32)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                sf.write(f.name, dummy, 16000)
                moonshine_onnx.transcribe(f.name, MOONSHINE_MODEL)
                os.unlink(f.name)
            elapsed = time.time() - start
            whisper_model = moonshine_onnx
            model_info = {
                "backend": "moonshine",
                "model": MOONSHINE_MODEL,
                "load_time": round(elapsed, 2),
            }
            print(f"Moonshine ONNX loaded in {elapsed:.2f}s")
            return
        except ImportError:
            if STT_BACKEND == "moonshine":
                raise RuntimeError("useful-moonshine-onnx not installed. Run: pip install useful-moonshine-onnx soundfile")
            print("moonshine_onnx not available, trying faster-whisper...")
        except Exception as e:
            if STT_BACKEND == "moonshine":
                raise
            print(f"Moonshine failed ({e}), trying faster-whisper...")

    # Try faster-whisper
    if STT_BACKEND in ("auto", "whisper"):
        try:
            from faster_whisper import WhisperModel

            compute_type = "float32" if WHISPER_DEVICE == "cpu" else "float16"
            print(f"Loading faster-whisper model: {WHISPER_MODEL} on {WHISPER_DEVICE}...")
            start = time.time()
            whisper_model = WhisperModel(
                WHISPER_MODEL,
                device=WHISPER_DEVICE,
                compute_type=compute_type,
            )
            elapsed = time.time() - start
            model_info = {
                "backend": "faster-whisper",
                "model": WHISPER_MODEL,
                "device": WHISPER_DEVICE,
                "compute_type": compute_type,
                "load_time": round(elapsed, 2),
            }
            print(f"Model loaded in {elapsed:.2f}s")
            return
        except ImportError:
            print("faster-whisper not available, trying openai-whisper...")

    # Fall back to openai-whisper
    try:
        import whisper

        print(f"Loading openai-whisper model: {WHISPER_MODEL}...")
        start = time.time()
        whisper_model = whisper.load_model(WHISPER_MODEL, device=WHISPER_DEVICE)
        elapsed = time.time() - start
        model_info = {
            "backend": "openai-whisper",
            "model": WHISPER_MODEL,
            "device": WHISPER_DEVICE,
            "load_time": round(elapsed, 2),
        }
        print(f"Model loaded in {elapsed:.2f}s")
        return
    except ImportError:
        raise RuntimeError("No Whisper backend available. Install faster-whisper or openai-whisper.")


def transcribe_audio(audio_path: str) -> dict:
    """Transcribe audio file using loaded model."""
    if whisper_model is None:
        raise RuntimeError("Model not loaded")

    start = time.time()
    backend = model_info.get("backend")

    if backend == "moonshine":
        texts = whisper_model.transcribe(audio_path, MOONSHINE_MODEL)
        text = " ".join(t.strip() for t in texts) if isinstance(texts, (list, tuple)) else str(texts).strip()
        result = {
            "text": text,
            "language": "en",
            "duration": None,
        }
    elif backend == "faster-whisper":
        segments, info = whisper_model.transcribe(
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
        result = whisper_model.transcribe(audio_path, language="en")
        result = {
            "text": result["text"].strip(),
            "language": result.get("language", "en"),
            "duration": None,
        }

    result["transcribe_time"] = round(time.time() - start, 2)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load model on startup."""
    load_whisper_model()
    print(f"STT server ready on {STT_HOST}:{STT_PORT}")
    yield
    print("Shutting down STT server...")


app = FastAPI(title="AgentWire STT Server", lifespan=lifespan)


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Transcribe audio file.

    Accepts WAV, MP3, M4A, WEBM, or any format ffmpeg can decode.
    Returns JSON with transcribed text.
    """
    if whisper_model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    # Determine file extension from content type or filename
    ext = ".wav"
    if file.filename:
        ext = Path(file.filename).suffix or ".wav"
    elif file.content_type:
        ext_map = {
            "audio/wav": ".wav",
            "audio/webm": ".webm",
            "audio/mp3": ".mp3",
            "audio/mpeg": ".mp3",
            "audio/m4a": ".m4a",
            "audio/x-m4a": ".m4a",
        }
        ext = ext_map.get(file.content_type, ".wav")

    # Save to temp file
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = transcribe_audio(tmp_path)
        return JSONResponse(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok" if whisper_model is not None else "loading",
        "model": model_info,
    }


@app.get("/capabilities")
async def capabilities():
    """Shim contract: capability discovery for agentwire.

    Plain transcription shim — no special input vocabulary, so `tool_prompt`
    is empty unless the operator sets AGENTWIRE_STT_TOOL_PROMPT.
    """
    import os

    return {
        "tool_prompt": os.environ.get("AGENTWIRE_STT_TOOL_PROMPT", ""),
        "model": model_info,
        "languages": ["en"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=STT_HOST, port=STT_PORT)
