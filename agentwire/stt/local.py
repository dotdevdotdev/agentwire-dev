"""In-process Moonshine for the default STT tier.

The portal owns one LocalMoonshine instance — the STT mirror of
``tts/local.py``'s LocalKokoro. When ``stt.backend: default``, a background
task pulls the Moonshine ONNX weights from HuggingFace (~200 MB, one-time)
and loads the model; until it's ready the portal keeps falling back to
browser SpeechRecognition. The ``custom`` shim tier never touches this module.

States:
    absent       model files not downloaded, warm-up not started
    downloading  background download in progress
    loading      files present, ONNX session loading
    ready        transcribe() available
    failed       download or load error (browser fallback stays active)
    unavailable  useful-moonshine-onnx not importable (py3.14+, ...) — terminal
"""

import asyncio
import importlib.util
import logging
import os
import wave
from pathlib import Path
from typing import Awaitable, Callable

from .base import STTBackend

logger = logging.getLogger(__name__)

# Default Moonshine variant. moonshine/base balances speed and accuracy on
# CPU; moonshine/tiny is faster/lighter. Operator override via MOONSHINE_MODEL
# mirrors the standalone shim (stt_server.py).
DEFAULT_MOONSHINE_MODEL = os.environ.get("MOONSHINE_MODEL", "moonshine/base")

_HF_REPO = "UsefulSensors/moonshine"
_ONNX_FILES = ("encoder_model", "decoder_model_merged")


def moonshine_importable() -> bool:
    """True if useful-moonshine-onnx is installed (base install, py<3.14)."""
    return importlib.util.find_spec("moonshine_onnx") is not None


def _model_files_cached(model_name: str) -> bool:
    """True if both ONNX weight files are already in the HuggingFace cache."""
    from huggingface_hub import try_to_load_from_cache

    short = model_name.split("/")[-1]
    return all(
        isinstance(
            try_to_load_from_cache(
                _HF_REPO, f"onnx/merged/{short}/float/{name}.onnx"
            ),
            str,
        )
        for name in _ONNX_FILES
    )


class LocalMoonshine:
    """Owns the default-tier Moonshine engine lifecycle for the portal."""

    def __init__(self, model_name: str = DEFAULT_MOONSHINE_MODEL) -> None:
        self.model_name = model_name
        self.state = "absent"
        self.percent = 0
        self.error: str | None = None
        self._model = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._on_change: Callable[["LocalMoonshine"], Awaitable[None]] | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def start(
        self, on_change: Callable[["LocalMoonshine"], Awaitable[None]] | None = None
    ) -> None:
        """Kick off the background download+load task (idempotent)."""
        if self._task is not None or self.state in ("ready", "unavailable"):
            return
        self._on_change = on_change
        if not moonshine_importable():
            self.state = "unavailable"
            self.error = "useful-moonshine-onnx not installed (requires Python <3.14)"
            logger.warning(f"Moonshine default STT unavailable: {self.error}")
            return
        self._task = asyncio.get_running_loop().create_task(self._warm_up())

    async def _notify(self) -> None:
        if self._on_change:
            try:
                await self._on_change(self)
            except Exception as e:
                logger.warning(f"Moonshine state-change callback failed: {e}")

    async def _set_state(self, state: str, percent: int | None = None) -> None:
        changed = state != self.state or (percent is not None and percent != self.percent)
        self.state = state
        if percent is not None:
            self.percent = percent
        if changed:
            await self._notify()

    async def _warm_up(self) -> None:
        try:
            if not await asyncio.to_thread(_model_files_cached, self.model_name):
                logger.info("Moonshine: downloading model files (~200 MB, one-time)...")
                await self._set_state("downloading", 0)
            else:
                await self._set_state("loading", 100)
            # MoonshineOnnxModel construction pulls weights from HF (if missing)
            # and loads the ONNX sessions — both happen here in one thread hop.
            self._model = await asyncio.to_thread(self._load_model)
            await self._set_state("ready")
            logger.info(f"Moonshine default STT ready ({self.model_name})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = str(e)
            logger.error(f"Moonshine warm-up failed: {e}")
            await self._set_state("failed")

    def _load_model(self):
        from moonshine_onnx import MoonshineOnnxModel

        return MoonshineOnnxModel(model_name=self.model_name)

    async def transcribe(self, audio_path: Path) -> str | None:
        """Transcribe a 16 kHz mono PCM16 WAV file to text.

        Serialized with a lock — one ONNX session, one transcription at a time.
        Returns None if the engine isn't ready or the clip is too short.
        """
        if not self.ready:
            return None
        async with self._lock:
            return await asyncio.to_thread(self._transcribe_sync, str(audio_path))

    def _transcribe_sync(self, audio_path: str) -> str | None:
        import numpy as np
        from moonshine_onnx import transcribe as moonshine_transcribe

        with wave.open(audio_path, "rb") as wav:
            channels = wav.getnchannels()
            frames = wav.readframes(wav.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        # Moonshine rejects clips outside 0.1s–64s; the portal feeds 16 kHz audio.
        if samples.size < 1600:
            return None
        # moonshine_transcribe's load_audio() adds the [batch] dim — pass 1-D.
        texts = moonshine_transcribe(samples, self._model)
        if isinstance(texts, (list, tuple)):
            return " ".join(t.strip() for t in texts).strip()
        return str(texts).strip()

    async def close(self) -> None:
        """Cancel the warm-up task and drop the model."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._model = None


class LocalMoonshineBackend(STTBackend):
    """STTBackend adapter that delegates to the portal's LocalMoonshine.

    Returned by ``get_stt_backend`` for the default tier when Moonshine is
    importable, so ``/transcribe`` transcribes on the host instead of 501-ing.
    """

    def __init__(self, moonshine: LocalMoonshine) -> None:
        self._moonshine = moonshine

    @property
    def name(self) -> str:
        return "moonshine"

    async def transcribe(self, audio_path: Path) -> str | None:
        return await self._moonshine.transcribe(audio_path)
