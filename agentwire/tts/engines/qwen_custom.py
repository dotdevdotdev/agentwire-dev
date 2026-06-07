"""Qwen3-TTS CustomVoice Engine"""

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

# Preset speakers with their native languages
PRESET_SPEAKERS = {
    "Vivian": "Chinese",      # Bright, slightly edgy young female
    "Serena": "Chinese",      # Warm, gentle young female
    "Uncle_Fu": "Chinese",    # Seasoned male, low mellow timbre
    "Dylan": "Chinese",       # Youthful Beijing male
    "Eric": "Chinese",        # Lively Chengdu male
    "Ryan": "English",        # Dynamic male, strong rhythm
    "Aiden": "English",       # Sunny American male
    "Ono_Anna": "Japanese",   # Playful Japanese female
    "Sohee": "Korean",        # Warm Korean female
}


class QwenCustomEngine(TTSEngine):
    """Qwen3-TTS CustomVoice model with preset premium voices.

    Supports:
    - 9 preset premium voices
    - Emotion/style control via instruct
    - 10 languages (each voice can speak any language)
    - Streaming output

    Preset voices: Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee
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

        model_id = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
        print("Loading Qwen3-TTS CustomVoice model...")

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

        print(f"Qwen3-TTS CustomVoice loaded! Sample rate: {self._sample_rate}")

    @property
    def name(self) -> str:
        return "Qwen3-TTS CustomVoice"

    @property
    def capabilities(self) -> TTSCapabilities:
        return TTSCapabilities(
            voice_cloning=False,
            voice_design=False,
            preset_voices=list(PRESET_SPEAKERS.keys()),
            emotion_control=True,  # Via instruct parameter
            paralinguistic_tags=False,
            streaming=True,
            languages=SUPPORTED_LANGUAGES,
        )

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def generate(self, request: TTSRequest) -> TTSResult:
        """Generate audio with preset voice.

        Args:
            request: TTS request with text, voice (preset speaker name), instruct (optional), language

        Returns:
            TTSResult with audio tensor

        Raises:
            ValueError: If voice not a valid preset speaker
        """
        speaker = request.voice or "Ryan"  # Default to Ryan (English)

        if speaker not in PRESET_SPEAKERS:
            available = ", ".join(PRESET_SPEAKERS.keys())
            raise ValueError(
                f"Unknown preset voice '{speaker}'. "
                f"Available: {available}"
            )

        wavs, sr = self._model.generate_custom_voice(
            text=request.text,
            language=request.language,
            speaker=speaker,
            instruct=request.instructions,  # Optional emotion/style
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

        speaker = request.voice or "Ryan"

        if speaker not in PRESET_SPEAKERS:
            available = ", ".join(PRESET_SPEAKERS.keys())
            raise ValueError(f"Unknown preset voice '{speaker}'. Available: {available}")

        wavs, sr = self._model.generate_custom_voice(
            text=request.text,
            language=request.language,
            speaker=speaker,
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
