"""In-process Kokoro for the default TTS tier.

The portal owns one LocalKokoro instance. When `tts.backend: default`, a
background task downloads the model files (~200 MB, one-time) and loads the
engine; until it's ready the portal keeps falling back to browser
speechSynthesis / OS voice. The `custom` shim tier never touches this module.

States:
    absent       model files not downloaded, warm-up not started
    downloading  background download in progress (see `percent`)
    loading      files present, ONNX session loading
    ready        synthesize() available
    failed       download or load error (fallback stays active)
    unavailable  kokoro-onnx not importable (py3.14+, musl, ...) — terminal
"""

import asyncio
import importlib.util
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Fallback sizes for progress math when the server omits Content-Length.
_APPROX_SIZES = {"kokoro-v1.0.fp16.onnx": 178_000_000, "voices-v1.0.bin": 28_000_000}


def kokoro_importable() -> bool:
    """True if the kokoro-onnx package is installed (base install, py<3.14)."""
    return importlib.util.find_spec("kokoro_onnx") is not None


class LocalKokoro:
    """Owns the default-tier Kokoro engine lifecycle for the portal."""

    def __init__(self) -> None:
        self.state = "absent"
        self.percent = 0
        self.error: str | None = None
        self._engine = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._on_change: Callable[["LocalKokoro"], Awaitable[None]] | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def start(
        self, on_change: Callable[["LocalKokoro"], Awaitable[None]] | None = None
    ) -> None:
        """Kick off the background download+load task (idempotent)."""
        if self._task is not None or self.state in ("ready", "unavailable"):
            return
        self._on_change = on_change
        if not kokoro_importable():
            self.state = "unavailable"
            self.error = "kokoro-onnx not installed (requires Python <3.14)"
            logger.warning(f"Kokoro default voice unavailable: {self.error}")
            return
        self._task = asyncio.get_running_loop().create_task(self._warm_up())

    async def _notify(self) -> None:
        if self._on_change:
            try:
                await self._on_change(self)
            except Exception as e:
                logger.warning(f"Kokoro state-change callback failed: {e}")

    async def _set_state(self, state: str, percent: int | None = None) -> None:
        changed = state != self.state or (percent is not None and percent != self.percent)
        self.state = state
        if percent is not None:
            self.percent = percent
        if changed:
            await self._notify()

    async def _warm_up(self) -> None:
        from .engines.kokoro import KokoroEngine

        loop = asyncio.get_running_loop()
        progress: dict[str, tuple[int, int]] = {}

        def _progress_cb(filename: str, downloaded: int, total: int) -> None:
            # Runs in the download thread — marshal to the event loop.
            progress[filename] = (downloaded, total or _APPROX_SIZES.get(filename, 0))
            done = sum(d for d, _ in progress.values())
            full = sum(t for _, t in progress.values()) + sum(
                size for name, size in _APPROX_SIZES.items() if name not in progress
            )
            pct = min(99, int(done * 100 / full)) if full else 0
            if pct >= self.percent + 5:
                loop.call_soon_threadsafe(
                    lambda p=pct: loop.create_task(self._set_state("downloading", p))
                )

        try:
            if not KokoroEngine.model_files_cached():
                logger.info("Kokoro: downloading model files (~200 MB, one-time)...")
                await self._set_state("downloading", 0)
                await asyncio.to_thread(KokoroEngine.download_models, _progress_cb)
            await self._set_state("loading", 100)
            self._engine = await asyncio.to_thread(KokoroEngine)
            await self._set_state("ready")
            logger.info("Kokoro default voice ready")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = str(e)
            logger.error(f"Kokoro warm-up failed: {e}")
            await self._set_state("failed")

    def resolve_voice(self, voice: str | None) -> str:
        """Map a configured voice name onto a Kokoro preset."""
        from .engines.kokoro import resolve_voice_name

        return resolve_voice_name(voice)

    async def synthesize(self, text: str, voice: str | None = None) -> tuple[bytes, float]:
        """Generate WAV bytes for text. Returns (wav_bytes, duration_seconds).

        Serialized with a lock — one ONNX session, one synthesis at a time.
        Raises RuntimeError if the engine isn't ready.
        """
        if not self.ready:
            raise RuntimeError(f"Kokoro engine not ready (state: {self.state})")

        from .audio import pcm_float_to_wav_bytes
        from .base import TTSRequest

        request = TTSRequest(text=text, voice=self.resolve_voice(voice))
        async with self._lock:
            result = await asyncio.to_thread(self._engine.generate, request)
        samples = result.audio.squeeze()
        duration = float(len(samples)) / result.sample_rate
        return pcm_float_to_wav_bytes(samples, result.sample_rate), duration

    async def close(self) -> None:
        """Cancel the warm-up task and unload the engine."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._engine is not None:
            self._engine.unload()
            self._engine = None
