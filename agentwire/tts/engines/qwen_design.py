"""Qwen3-TTS VoiceDesign Engine"""

from pathlib import Path
from typing import Iterator

import numpy as np
import torch

from ..base import TTSCapabilities, TTSEngine, TTSRequest, TTSResult

SUPPORTED_LANGUAGES = [
    "Chinese",
    "English",
    "Japanese",
    "Korean",
    "German",
    "French",
    "Russian",
    "Portuguese",
    "Spanish",
    "Italian",
]


class QwenDesignEngine(TTSEngine):
    """Qwen3-TTS VoiceDesign model for generating voices from descriptions.

    Supports:
    - Voice design from natural language descriptions
    - 10 languages
    - Streaming output

    Example instruct prompts:
    - "Deep male voice with warm tone, speaking slowly"
    - "Young excited female voice, high energy"
    - "Calm, soothing voice for meditation"
    """

    def __init__(
        self,
        device: str = "cuda:0",
        dtype: torch.dtype = torch.bfloat16,
        compile_model: bool = True,
        compile_mode: str = "reduce-overhead",
        voices_dir: Path | None = None,
    ):
        from qwen_tts import Qwen3TTSModel

        model_id = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
        print("Loading Qwen3-TTS VoiceDesign model...")

        # Check for FlashAttention
        try:
            import flash_attn  # noqa: F401

            attn_impl = "flash_attention_2"
            print("  Using FlashAttention 2")
        except ImportError:
            attn_impl = "sdpa"
            print("  Using SDPA")

        self._model = Qwen3TTSModel.from_pretrained(
            model_id,
            device_map=device,
            dtype=dtype,
            attn_implementation=attn_impl,
        )
        self._sample_rate = 24000
        self._voices_dir = voices_dir

        # Apply torch.compile
        if compile_model:
            try:
                if hasattr(self._model, "model"):
                    print(f"  Applying torch.compile ({compile_mode} mode)...")
                    self._model.model = torch.compile(
                        self._model.model,
                        mode=compile_mode,
                    )
                    print("  torch.compile applied!")
            except Exception as e:
                print(f"  torch.compile failed: {e}")

        print(f"Qwen3-TTS VoiceDesign loaded! Sample rate: {self._sample_rate}")

    @property
    def name(self) -> str:
        return "Qwen3-TTS VoiceDesign"

    @property
    def capabilities(self) -> TTSCapabilities:
        return TTSCapabilities(
            voice_cloning=False,
            voice_design=True,
            preset_voices=[],
            emotion_control=True,  # Via instruct parameter
            paralinguistic_tags=False,
            streaming=True,
            languages=SUPPORTED_LANGUAGES,
        )

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def generate(self, request: TTSRequest) -> TTSResult:
        """Generate audio with designed voice.

        Args:
            request: TTS request with text, instruct (voice description), language

        Returns:
            TTSResult with audio tensor

        Raises:
            ValueError: If no instruct provided
        """
        if not request.instructions:
            raise ValueError(
                "Qwen3-TTS VoiceDesign requires an 'instructions' parameter describing the voice. "
                "Example: 'Deep male voice with warm tone'"
            )

        wavs, sr = self._model.generate_voice_design(
            text=request.text,
            language=request.language,
            instruct=request.instructions,
        )

        # Convert to tensor
        wav = wavs[0] if isinstance(wavs, list) else wavs
        if isinstance(wav, np.ndarray):
            wav = torch.from_numpy(wav)
        if wav.dim() == 1:
            wav = wav.unsqueeze(0)

        return TTSResult(audio=wav, sample_rate=sr)

    def generate_stream(self, request: TTSRequest) -> Iterator[bytes]:
        """Generate audio as streaming chunks."""
        import io

        import torchaudio

        if not request.instructions:
            raise ValueError("Qwen3-TTS VoiceDesign requires an 'instructions' parameter.")

        wavs, sr = self._model.generate_voice_design(
            text=request.text,
            language=request.language,
            instruct=request.instructions,
            non_streaming_mode=False,
        )

        wav = wavs[0] if isinstance(wavs, list) else wavs
        if isinstance(wav, np.ndarray):
            wav = torch.from_numpy(wav)
        if wav.dim() == 1:
            wav = wav.unsqueeze(0)

        buffer = io.BytesIO()
        torchaudio.save(buffer, wav, sr, format="wav")
        buffer.seek(0)
        yield buffer.read()

    def unload(self) -> None:
        """Release model from GPU memory."""
        if hasattr(self, "_model"):
            del self._model
            self._model = None
