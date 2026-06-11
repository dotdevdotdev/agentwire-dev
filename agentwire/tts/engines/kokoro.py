"""Kokoro TTS Engine (kokoro-onnx) - CPU-only, ultra-lightweight, torch-free."""

import asyncio
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from ..audio import pcm_float_to_wav_bytes
from ..base import TTSCapabilities, TTSEngine, TTSRequest, TTSResult

# Preset voices bundled with Kokoro v1.0
# Full list: https://huggingface.co/hexgrad/Kokoro-82M-ONNX
PRESET_VOICES = [
    # American English (female)
    "af_heart",
    "af_bella",
    "af_nicole",
    "af_sky",
    "af_sarah",
    "af_alloy",
    "af_aoede",
    "af_jessica",
    "af_kore",
    "af_nova",
    "af_river",
    # American English (male)
    "am_adam",
    "am_michael",
    "am_echo",
    "am_eric",
    "am_liam",
    "am_onyx",
    "am_puck",
    # British English (female)
    "bf_emma",
    "bf_isabella",
    # British English (male)
    "bm_george",
    "bm_lewis",
    # Spanish
    "ef_dora",
    # French
    "ff_siwis",
    # Hindi
    "hf_alpha",
    "hf_beta",
    # Italian
    "im_nicola",
    # Japanese
    "jf_alpha",
    "jf_gongitsune",
    # Portuguese
    "pf_dora",
    # Chinese
    "zf_xiaobei",
    "zf_xiaoni",
]

SUPPORTED_LANGUAGES = [
    "English",
    "Spanish",
    "French",
    "Hindi",
    "Italian",
    "Japanese",
    "Portuguese",
    "Chinese",
]

_LANG_MAP = {
    "English": "en-us",
    "Spanish": "es",
    "French": "fr-fr",
    "Hindi": "hi",
    "Italian": "it",
    "Japanese": "ja",
    "Portuguese": "pt-br",
    "Chinese": "zh",
}

DEFAULT_VOICE = "af_heart"


def resolve_voice_name(voice: str | None) -> str:
    """Map any configured voice name onto a Kokoro preset.

    Known preset → itself; "random" → random preset; anything else
    (cloned-voice names from other backends, "default") → af_heart.
    """
    if voice and voice in PRESET_VOICES:
        return voice
    if voice and voice.lower() == "random":
        import random

        return random.choice(PRESET_VOICES)
    return DEFAULT_VOICE


class KokoroEngine(TTSEngine):
    """Kokoro TTS engine via kokoro-onnx.

    Ultra-lightweight CPU-only TTS:
    - ~82M parameters, ~170 MB ONNX model (fp16)
    - No GPU required — pure ONNX CPU inference
    - Near real-time on Apple Silicon / modern Intel CPU
    - 30+ preset voices across 8 languages
    - Streaming support
    - Model auto-downloaded from GitHub releases on first use (~170 MB, cached in ~/.cache/kokoro_onnx/)
    - Torch-free: ships with the base install (kokoro-onnx + onnxruntime)
    """

    # GitHub release URL for model files. fp16 is half the fp32 download
    # (~170 MB vs ~325 MB) with no audible difference on CPU inference.
    _MODEL_FILE = "kokoro-v1.0.fp16.onnx"
    _MODEL_URL = f"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/{_MODEL_FILE}"
    _VOICES_FILE = "voices-v1.0.bin"
    _VOICES_URL = f"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/{_VOICES_FILE}"

    def __init__(self, voices_dir: Path | None = None):
        from kokoro_onnx import Kokoro

        model_path = self._ensure_file(self._MODEL_FILE, self._MODEL_URL)
        voices_path = self._ensure_file(self._VOICES_FILE, self._VOICES_URL)

        print("Loading Kokoro ONNX model...")
        self._model = Kokoro(str(model_path), str(voices_path))
        self._voices_dir = voices_dir
        self._sample_rate = 24000
        print("Kokoro loaded!")

    @classmethod
    def download_models(
        cls, progress_cb: Callable[[str, int, int], None] | None = None
    ) -> None:
        """Download the model + voices files if missing (blocking).

        Public entry point for the portal's background warm-up and the
        `agentwire tts warm` CLI command.
        """
        cls._ensure_file(cls._MODEL_FILE, cls._MODEL_URL, progress_cb)
        cls._ensure_file(cls._VOICES_FILE, cls._VOICES_URL, progress_cb)

    @classmethod
    def model_files_cached(cls) -> bool:
        """True if both model files are already downloaded."""
        cache_dir = Path.home() / ".cache" / "kokoro_onnx"
        return (cache_dir / cls._MODEL_FILE).exists() and (
            cache_dir / cls._VOICES_FILE
        ).exists()

    @staticmethod
    def _ensure_file(
        filename: str,
        url: str,
        progress_cb: Callable[[str, int, int], None] | None = None,
    ) -> Path:
        """Download file to ~/.cache/kokoro_onnx/ if not already present.

        Downloads to a .part file and renames atomically so an interrupted
        download never leaves a truncated file that passes the exists() check.

        Args:
            progress_cb: Optional callback(filename, downloaded_bytes, total_bytes)
                invoked as the download progresses (total is 0 if unknown).
        """
        import urllib.request

        cache_dir = Path.home() / ".cache" / "kokoro_onnx"
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / filename

        if not dest.exists():
            size_mb = 170 if "onnx" in filename else 10
            print(f"Downloading {filename} (~{size_mb} MB)...")
            part = dest.with_suffix(dest.suffix + ".part")

            def _hook(block_num: int, block_size: int, total_size: int) -> None:
                if progress_cb:
                    downloaded = min(block_num * block_size, max(total_size, 0)) \
                        if total_size > 0 else block_num * block_size
                    progress_cb(filename, downloaded, max(total_size, 0))

            import socket

            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(60)
            try:
                urllib.request.urlretrieve(url, part, reporthook=_hook)
                part.replace(dest)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            finally:
                socket.setdefaulttimeout(old_timeout)
            print(f"Saved to {dest}")

        return dest

    @property
    def name(self) -> str:
        return "Kokoro"

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def capabilities(self) -> TTSCapabilities:
        return TTSCapabilities(
            voice_cloning=False,
            voice_design=False,
            preset_voices=PRESET_VOICES,
            emotion_control=False,
            paralinguistic_tags=False,
            streaming=True,
            languages=SUPPORTED_LANGUAGES,
        )

    def _resolve_voice(self, request: TTSRequest) -> str:
        """Return a valid Kokoro voice name, falling back to default.

        If request.voice isn't a known preset (e.g. user has voice: dotdev from
        a different backend config), we silently fall back to af_heart.
        """
        if request.voice and request.voice in PRESET_VOICES:
            return request.voice
        return DEFAULT_VOICE

    def generate(self, request: TTSRequest) -> TTSResult:
        voice = self._resolve_voice(request)
        lang = _LANG_MAP.get(request.language, "en-us")

        samples, sample_rate = self._model.create(
            text=request.text,
            voice=voice,
            speed=1.0,
            lang=lang,
        )

        # numpy (N,) → (1, N) to match the TTSResult audio shape convention
        audio = np.asarray(samples, dtype=np.float32)[None, :]
        return TTSResult(audio=audio, sample_rate=sample_rate)

    def generate_stream(self, request: TTSRequest) -> Iterator[bytes]:
        """Yield WAV chunks from Kokoro's async streaming generator.

        kokoro-onnx create_stream() is an async generator; we drive it
        chunk-by-chunk from a dedicated event loop so the sync TTSEngine
        interface is preserved.

        Uses stdlib wave for WAV serialization (avoids torchaudio's BytesIO
        incompatibility with the torchcodec backend in CPU-only builds).
        """
        voice = self._resolve_voice(request)
        lang = _LANG_MAP.get(request.language, "en-us")

        loop = asyncio.new_event_loop()
        async_gen = self._model.create_stream(
            text=request.text,
            voice=voice,
            speed=1.0,
            lang=lang,
        )

        try:
            while True:
                try:
                    samples, sample_rate = loop.run_until_complete(async_gen.__anext__())
                except StopAsyncIteration:
                    break

                yield pcm_float_to_wav_bytes(samples, sample_rate)
        finally:
            loop.close()

    def unload(self) -> None:
        if hasattr(self, "_model"):
            del self._model
            self._model = None
