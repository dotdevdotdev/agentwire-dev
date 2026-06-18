#!/usr/bin/env python3
"""AgentWire Kokoro Server — the process-isolated default TTS tier.

The default voice tier runs Kokoro (kokoro-onnx, torch-free, base install) in
this **standalone shim subprocess** (tmux ``agentwire-kokoro``, ``:8102``),
which the portal auto-manages via ``ensure_managed_tts``. Process isolation
keeps the ~200 MB one-time model download and the GIL-holding ONNX warm-up off
the portal's event loop — the #382 failure mode the STT shim (``:8101``) fixed
for Moonshine, mirrored here for Kokoro (#398).

The engine lifecycle (download → load → ready, plus serialized synthesis) lives
in ``LocalKokoro``; this module is the thin HTTP wrapper. The portal talks to
it over the same shim contract envelope it already uses for the custom tier
(``/tts`` with ``{text, voice, instructions, options}``); ``/health`` reports
the warm-up state so the portal keeps browser speechSynthesis as the fallback
until the model is ready.

Run via:
    agentwire kokoro start                  # Start in tmux (CPU)
    agentwire kokoro stop                    # Stop the server
    agentwire kokoro status                  # Check status

Or run directly:
    KOKORO_PORT=8102 uvicorn agentwire.tts.kokoro_server:app --host 0.0.0.0 --port 8102
"""

import io
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from .base import TTSRequest
from .engines.kokoro import PRESET_VOICES, SUPPORTED_LANGUAGES
from .local import LocalKokoro

# Configuration via environment (mirrors stt_server's STT_HOST/STT_PORT).
KOKORO_HOST = os.environ.get("KOKORO_HOST", "0.0.0.0")
KOKORO_PORT = int(os.environ.get("KOKORO_PORT", "8102"))

# Single engine owned by this process. Constructed at import (cheap, no I/O);
# the warm-up download+load is kicked off in the lifespan startup.
kokoro = LocalKokoro()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Kick off the background warm-up on startup.

    ``LocalKokoro.start`` downloads the model files (~200 MB, one-time) and
    loads the ONNX session in a background task. This holds the GIL — which is
    exactly why it must run here, in the shim, and not in the portal."""
    kokoro.start()
    print(f"Kokoro server starting on {KOKORO_HOST}:{KOKORO_PORT} (warming up)")
    yield
    await kokoro.close()
    print("Shutting down Kokoro server...")


app = FastAPI(title="AgentWire Kokoro Server", lifespan=lifespan)


@app.post("/tts")
async def generate_tts(request: TTSRequest):
    """Generate TTS audio from text.

    Accepts the agentwire shim contract envelope: core ``text``/``voice`` plus
    opaque ``instructions`` and ``options`` (ignored — Kokoro takes no style
    knobs). Returns WAV bytes, matching the custom shim's ``/tts``.
    """
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    if not kokoro.ready:
        raise HTTPException(
            status_code=503, detail=f"Kokoro not ready (state: {kokoro.state})"
        )

    try:
        wav_bytes, _ = await kokoro.synthesize(request.text, request.voice)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return StreamingResponse(
        io.BytesIO(wav_bytes),
        media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=speech.wav"},
    )


@app.get("/health")
async def health():
    """Health check — surfaces the warm-up state machine.

    ``status`` is ``ok`` once the engine is ready; until then it mirrors
    ``LocalKokoro.state`` (``absent``/``downloading``/``loading``/``failed``/
    ``unavailable``) so the portal can keep browser speech as the fallback and
    render download progress from ``percent``.
    """
    status = "ok" if kokoro.ready else kokoro.state
    body: dict = {"status": status, "engine": "kokoro", "percent": kokoro.percent}
    if kokoro.error:
        body["error"] = kokoro.error
    return body


@app.get("/voices")
async def list_voices():
    """List the Kokoro preset voices (available once the engine is ready)."""
    return {"voices": list(PRESET_VOICES) if kokoro.ready else []}


@app.get("/capabilities")
async def capabilities():
    """Shim contract: capability discovery for agentwire.

    Kokoro is a plain preset-voice engine — no emotion control, no
    paralinguistic tags — so ``tool_prompt`` is empty unless the operator sets
    AGENTWIRE_KOKORO_TOOL_PROMPT.
    """
    return {
        "tool_prompt": os.environ.get("AGENTWIRE_KOKORO_TOOL_PROMPT", ""),
        "voices": list(PRESET_VOICES) if kokoro.ready else [],
        "engine": "kokoro",
        "emotion_control": False,
        "paralinguistic_tags": False,
        "voice_cloning": False,
        "languages": SUPPORTED_LANGUAGES,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=KOKORO_HOST, port=KOKORO_PORT)
