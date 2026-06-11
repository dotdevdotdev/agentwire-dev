#!/usr/bin/env python3
"""AgentWire TTS Server - Multi-backend TTS with Hot-Swap Support

Supported backends:
  - chatterbox: Chatterbox Turbo TTS (default, fastest)
  - chatterbox-streaming: Chatterbox with streaming support
  - qwen-base-0.6b: Qwen3-TTS 0.6B (voice cloning)
  - qwen-base-1.7b: Qwen3-TTS 1.7B (voice cloning, higher quality)
  - qwen-design: Qwen3-TTS VoiceDesign (generate voices from descriptions)
  - qwen-custom: Qwen3-TTS CustomVoice (preset voices with emotion control)
  - zonos-hybrid: Zonos v0.1 SSM-Hybrid (<4 GB VRAM, emotion control, 5 languages)
  - zonos-transformer: Zonos v0.1 Transformer (standard arch variant)
  - kokoro: Kokoro 82M ONNX (CPU-only, ~170 MB, 30+ voices, streaming)

Run via:
    agentwire tts start                      # Start with default backend
    agentwire tts start --backend qwen-base-1.7b  # Start with specific backend
    agentwire tts stop                       # Stop the server
    agentwire tts status                     # Check status

Or run directly:
    DEFAULT_BACKEND=chatterbox uvicorn agentwire.tts_server:app --host 0.0.0.0 --port 8100

Hot-swap backends via API:
    POST /engines/qwen-base-1.7b/load        # Switch to Qwen 1.7B
    GET /engines                             # List available engines
"""

import io
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import torch
import torchaudio
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from faster_whisper import WhisperModel

from .tts import TTSRequest, registry

# GPU Optimizations
torch.backends.cudnn.benchmark = True
torch.set_float32_matmul_precision("high")

# Configuration via environment
DEFAULT_BACKEND = os.environ.get("DEFAULT_BACKEND", "chatterbox")
CURRENT_VENV = os.environ.get("CURRENT_VENV", "unknown")
VOICES_DIR = Path(os.environ.get("VOICES_DIR", str(Path.home() / ".agentwire" / "voices")))

# Backend family mapping - which venv each backend requires
BACKEND_FAMILIES = {
    "chatterbox": "chatterbox",
    "chatterbox-streaming": "chatterbox",
    "qwen-base-0.6b": "qwen",
    "qwen-base-1.7b": "qwen",
    "qwen-design": "qwen",
    "qwen-custom": "qwen",
    "zonos-hybrid": "zonos",
    "zonos-transformer": "zonos",
    "kokoro": "kokoro",
}

# Global Whisper model (separate from TTS engines)
whisper_model = None


def _tensor_to_wav_bytes(audio, sample_rate: int) -> bytes:
    """Serialize engine audio (torch.Tensor or np.ndarray) to WAV bytes.

    Kokoro returns numpy (torch-free engine) — serialize directly via stdlib
    wave. Torch tensors go through torchaudio.save, falling back to stdlib
    wave if it can't write to BytesIO (e.g. when torchcodec is the backend
    and doesn't support in-memory writes — happens with CPU-only builds).
    """
    import io

    import numpy as np

    from .tts.audio import pcm_float_to_wav_bytes

    if isinstance(audio, np.ndarray):
        return pcm_float_to_wav_bytes(audio, sample_rate)

    buf = io.BytesIO()
    try:
        torchaudio.save(buf, audio, sample_rate, format="wav")
        if buf.tell() == 0:
            raise RuntimeError("torchaudio.save wrote 0 bytes")
        buf.seek(0)
        return buf.read()
    except Exception:
        # Fallback: stdlib wave (PCM int16)
        return pcm_float_to_wav_bytes(audio.squeeze().numpy(), sample_rate)


def get_required_venv(backend: str) -> str:
    """Get the venv family required for a backend."""
    return BACKEND_FAMILIES.get(backend, "unknown")


def check_venv_compatibility(backend: str) -> tuple[bool, str]:
    """Check if current venv can run the requested backend.

    Returns:
        (compatible, required_venv)
    """
    required = get_required_venv(backend)
    if CURRENT_VENV == "unknown":
        # Unknown venv - try anyway
        return True, required
    return CURRENT_VENV == required, required


def register_engines():
    """Register all available TTS engine factories."""
    # Set voices directory for all engines
    registry._voices_dir = VOICES_DIR

    # Chatterbox engines
    def make_chatterbox():
        from .tts.engines.chatterbox import ChatterboxEngine
        return ChatterboxEngine(voices_dir=VOICES_DIR)

    def make_chatterbox_streaming():
        from .tts.engines.chatterbox import ChatterboxStreamingEngine
        return ChatterboxStreamingEngine(voices_dir=VOICES_DIR)

    # Qwen engines
    def make_qwen_base_06b():
        from .tts.engines.qwen_base import QwenBaseEngine
        return QwenBaseEngine(model_size="0.6b", voices_dir=VOICES_DIR)

    def make_qwen_base_17b():
        from .tts.engines.qwen_base import QwenBaseEngine
        return QwenBaseEngine(model_size="1.7b", voices_dir=VOICES_DIR)

    def make_qwen_design():
        from .tts.engines.qwen_design import QwenDesignEngine
        return QwenDesignEngine(voices_dir=VOICES_DIR)

    def make_qwen_custom():
        from .tts.engines.qwen_custom import QwenCustomEngine
        return QwenCustomEngine(voices_dir=VOICES_DIR)

    # Zonos engines
    def make_zonos_hybrid():
        from .tts.engines.zonos import ZonosHybridEngine
        return ZonosHybridEngine(voices_dir=VOICES_DIR)

    def make_zonos_transformer():
        from .tts.engines.zonos import ZonosTransformerEngine
        return ZonosTransformerEngine(voices_dir=VOICES_DIR)

    # Register all engines
    registry.register("chatterbox", make_chatterbox)
    registry.register("chatterbox-streaming", make_chatterbox_streaming)
    registry.register("qwen-base-0.6b", make_qwen_base_06b)
    registry.register("qwen-base-1.7b", make_qwen_base_17b)
    registry.register("qwen-design", make_qwen_design)
    registry.register("qwen-custom", make_qwen_custom)
    registry.register("zonos-hybrid", make_zonos_hybrid)
    registry.register("zonos-transformer", make_zonos_transformer)

    # Kokoro engine (CPU-only)
    def make_kokoro():
        from .tts.engines.kokoro import KokoroEngine

        return KokoroEngine(voices_dir=VOICES_DIR)

    registry.register("kokoro", make_kokoro)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize server on startup."""
    global whisper_model
    VOICES_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Default Backend: {DEFAULT_BACKEND}")
    print(f"  cuDNN benchmark: {torch.backends.cudnn.benchmark}")
    print(f"  TF32 matmul: {torch.get_float32_matmul_precision()}")

    # Register all engine factories
    register_engines()
    print(f"Registered engines: {', '.join(registry.available)}")

    # Load default engine
    try:
        engine = registry.load(DEFAULT_BACKEND)
        print(f"Loaded engine: {engine.name}")
        if engine.capabilities.paralinguistic_tags:
            print("Paralinguistic tags supported: [laugh], [chuckle], [cough], [sigh], [gasp]")
    except Exception as e:
        print(f"Failed to load default engine: {e}")

    # Load Whisper for transcription
    print("Loading Whisper model (large-v3)...")
    try:
        whisper_model = WhisperModel("large-v3", device="cuda", compute_type="float16")
    except (ValueError, RuntimeError):
        # CUDA unavailable (e.g. CPU-only venv) — fall back to CPU
        whisper_model = WhisperModel("large-v3", device="cpu", compute_type="int8")
    print("Whisper model loaded!")

    print(f"Voices directory: {VOICES_DIR}")
    yield

    # Cleanup
    print("Shutting down...")
    registry.unload_current()


app = FastAPI(title="AgentWire TTS Server", lifespan=lifespan)


# === TTS Endpoints ===


# Contract `options` keys this shim understands — folded into TTSRequest
# fields before engine dispatch. Unknown keys are ignored (envelope doctrine:
# the caller passes options verbatim; the shim takes what it knows).
KNOWN_OPTION_FIELDS = {
    "exaggeration", "cfg_weight", "language", "backend", "stream",
    "speaking_rate", "pitch_std",
    "emotion_happiness", "emotion_sadness", "emotion_disgust", "emotion_fear",
    "emotion_surprise", "emotion_anger", "emotion_other",
}


@app.post("/tts")
async def generate_tts(request: TTSRequest):
    """Generate TTS audio from text.

    Accepts the agentwire shim contract envelope: core `text`/`voice` plus
    opaque `instructions` and `options`. Known option keys map onto engine
    parameters; `options.backend` hot-swaps engines.
    """
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    # Fold known contract options into request fields (explicit fields win
    # only when options doesn't name them; options is the envelope source)
    if request.options:
        for key, value in request.options.items():
            if key in KNOWN_OPTION_FIELDS:
                setattr(request, key, value)

    # Determine which backend to use
    backend = request.backend or registry.current_name or DEFAULT_BACKEND

    # Check if current venv can handle this backend
    compatible, required_venv = check_venv_compatibility(backend)
    if not compatible:
        # Return special error that CLI can catch and handle
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=422,
            content={
                "error": "venv_mismatch",
                "message": f"Backend '{backend}' requires venv '{required_venv}', but server is running in '{CURRENT_VENV}'",
                "current_venv": CURRENT_VENV,
                "required_venv": required_venv,
                "backend": backend,
            }
        )

    try:
        # Hot-swap if needed
        engine = registry.get_or_load(backend)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Validate voice exists (for cloning backends)
    if request.voice and engine.capabilities.voice_cloning:
        voice_path = registry.get_voice_path(request.voice)
        if not voice_path:
            raise HTTPException(
                status_code=404,
                detail=f"Voice '{request.voice}' not found"
            )

    try:
        # Generate audio
        if request.stream and engine.capabilities.streaming:
            # Streaming response
            return StreamingResponse(
                engine.generate_stream(request),
                media_type="audio/wav",
                headers={"Content-Disposition": "attachment; filename=speech.wav"},
            )
        else:
            # Non-streaming response
            result = engine.generate(request)
            wav_bytes = _tensor_to_wav_bytes(result.audio, result.sample_rate)
            return StreamingResponse(
                io.BytesIO(wav_bytes),
                media_type="audio/wav",
                headers={"Content-Disposition": "attachment; filename=speech.wav"},
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Engine Management Endpoints ===


@app.get("/engines")
async def list_engines():
    """List available TTS engines and current state."""
    return {
        "available": registry.available,
        "current": registry.current_name,
        "capabilities": asdict(registry.current_capabilities) if registry.current_capabilities else None,
    }


@app.post("/engines/{name}/load")
async def load_engine(name: str):
    """Load a specific TTS engine (hot-swap)."""
    try:
        engine = registry.load(name)
        return {
            "loaded": name,
            "engine_name": engine.name,
            "capabilities": asdict(engine.capabilities),
        }
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/engines/unload")
async def unload_engine():
    """Unload current engine to free GPU memory."""
    registry.unload_current()
    return {"message": "Engine unloaded", "current": None}


# === Voice Management Endpoints ===


@app.get("/voices")
async def list_voices():
    """List all available voice profiles."""
    voices = []
    for f in VOICES_DIR.glob("*.wav"):
        waveform, sr = torchaudio.load(str(f))
        duration = waveform.shape[1] / sr
        voices.append({"name": f.stem, "duration": round(duration, 2)})
    return {"voices": voices}


@app.post("/voices/{name}")
async def upload_voice(name: str, file: UploadFile = File(...)):
    """Upload a voice reference audio (~10s WAV recommended)."""
    # Validate name
    valid_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if not all(c in valid_chars for c in name):
        raise HTTPException(
            status_code=400,
            detail="Voice name must contain only alphanumeric characters, underscores, and hyphens"
        )

    voice_path = VOICES_DIR / f"{name}.wav"
    content = await file.read()

    # Convert to proper format (24kHz mono)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        waveform, sr = torchaudio.load(tmp_path)

        # Convert to mono if stereo
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)

        # Resample to 24kHz if needed
        if sr != 24000:
            resampler = torchaudio.transforms.Resample(sr, 24000)
            waveform = resampler(waveform)

        # Save processed audio
        torchaudio.save(str(voice_path), waveform, 24000)

        duration = waveform.shape[1] / 24000
        return {
            "name": name,
            "duration": round(duration, 2),
            "message": f"Voice '{name}' saved ({duration:.1f}s)",
        }
    finally:
        os.unlink(tmp_path)


@app.delete("/voices/{name}")
async def delete_voice(name: str):
    """Delete a voice profile."""
    voice_path = VOICES_DIR / f"{name}.wav"
    if not voice_path.exists():
        raise HTTPException(status_code=404, detail=f"Voice '{name}' not found")
    voice_path.unlink()
    return {"message": f"Voice '{name}' deleted"}


# === Transcription Endpoint ===


@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """Transcribe audio using Whisper."""
    if whisper_model is None:
        raise HTTPException(status_code=503, detail="Whisper model not loaded")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        segments, info = whisper_model.transcribe(
            tmp_path,
            beam_size=5,
            language="en",
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments)
        return {
            "text": text,
            "language": info.language,
            "duration": info.duration,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)


# === Health Check ===


@app.get("/health")
async def health():
    """Health check with engine status."""
    return {
        "status": "ok",
        "engine": registry.current_name,
        "engine_name": registry.current.name if registry.current else None,
        "capabilities": asdict(registry.current_capabilities) if registry.current_capabilities else None,
        "available_engines": registry.available,
        "whisper_loaded": whisper_model is not None,
    }


def _default_tool_prompt(caps) -> str:
    """Derive a `tool_prompt` from engine capabilities when the operator
    hasn't set AGENTWIRE_TTS_TOOL_PROMPT. This text is injected verbatim into
    the agent's `say` tooldef, so it speaks to the agent."""
    parts = []
    if caps and caps.paralinguistic_tags:
        parts.append(
            "This TTS supports inline paralinguistic tags like [laugh], "
            "[chuckle], [sigh], [gasp] — use them sparingly for expressive delivery."
        )
    if caps and caps.emotion_control:
        parts.append(
            "You may pass an `instructions` field with a free-text style prompt "
            "(e.g. \"speak warmly, slightly amused\") to shape delivery."
        )
    return " ".join(parts)


@app.get("/capabilities")
async def capabilities():
    """Shim contract: capability discovery for agentwire.

    `tool_prompt` is appended to the agent-facing `say` tooldef at MCP-server
    start. Override with the AGENTWIRE_TTS_TOOL_PROMPT env var.
    """
    caps = registry.current_capabilities
    voices = [f.stem for f in VOICES_DIR.glob("*.wav")]
    return {
        "tool_prompt": os.environ.get("AGENTWIRE_TTS_TOOL_PROMPT") or _default_tool_prompt(caps),
        "voices": voices,
        "engine": registry.current_name,
        "emotion_control": bool(caps and caps.emotion_control),
        "paralinguistic_tags": bool(caps and caps.paralinguistic_tags),
        "voice_cloning": bool(caps and caps.voice_cloning),
        "languages": caps.languages if caps else ["English"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
