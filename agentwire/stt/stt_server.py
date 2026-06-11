#!/usr/bin/env python3
"""AgentWire STT Server - Persistent transcription endpoint.

Keeps the local model loaded in memory to eliminate cold start delays
(local use avoids audio upload latency), or proxies to the OpenAI
transcription API when ``STT_BACKEND=cloud-openai`` is selected — for
hosts that can't run local STT well. The OpenAI key comes from the
server environment (``OPENAI_API_KEY``) and never reaches the browser.

Run via:
    agentwire stt start                     # Start in tmux (CPU)
    agentwire stt start --model large-v3    # Specific model
    agentwire stt start --backend cloud-openai  # OpenAI transcription API
    agentwire stt stop                      # Stop the server
    agentwire stt status                    # Check status

Or run directly:
    WHISPER_MODEL=base uvicorn agentwire.stt.stt_server:app --host 0.0.0.0 --port 8101

Backend selection and transcription logic live in ``engine.py`` (kept
FastAPI-free for unit testing); this module is the HTTP wrapper.
"""

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from . import engine

# Configuration via environment
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
STT_HOST = os.environ.get("STT_HOST", "0.0.0.0")
STT_PORT = int(os.environ.get("STT_PORT", "8101"))
# moonshine | whisper | cloud-openai | auto (moonshine → faster-whisper →
# openai-whisper → cloud-openai when OPENAI_API_KEY is set)
STT_BACKEND = os.environ.get("STT_BACKEND", "auto")
# Moonshine model: moonshine/tiny (fastest) or moonshine/base (better accuracy)
MOONSHINE_MODEL = os.environ.get("MOONSHINE_MODEL", "moonshine/base")

# Global backend state: stt_model is None for cloud-openai; a non-empty
# model_info means the backend is ready.
stt_model = None
model_info = {}


def load_stt_backend():
    """Load the STT backend based on environment config."""
    global stt_model, model_info
    stt_model, model_info = engine.load_backend(
        backend=STT_BACKEND,
        whisper_model=WHISPER_MODEL,
        whisper_device=WHISPER_DEVICE,
        moonshine_model=MOONSHINE_MODEL,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load backend on startup."""
    load_stt_backend()
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
    if not model_info:
        raise HTTPException(status_code=503, detail="Backend not loaded")

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
        result = engine.transcribe(stt_model, model_info, tmp_path)
        return JSONResponse(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok" if model_info else "loading",
        "model": model_info,
    }


@app.get("/capabilities")
async def capabilities():
    """Shim contract: capability discovery for agentwire.

    Plain transcription shim — no special input vocabulary, so `tool_prompt`
    is empty unless the operator sets AGENTWIRE_STT_TOOL_PROMPT.
    """
    return {
        "tool_prompt": os.environ.get("AGENTWIRE_STT_TOOL_PROMPT", ""),
        "model": model_info,
        "languages": ["en"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=STT_HOST, port=STT_PORT)
