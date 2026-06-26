"""Zonos TTS Engine (Zyphra)"""

from pathlib import Path
from typing import Iterator

from ..base import TTSCapabilities, TTSEngine, TTSRequest, TTSResult

SUPPORTED_LANGUAGES = ["English", "Japanese", "Chinese", "French", "German"]

# BCP-47 language codes for Zonos
_LANGUAGE_MAP = {
    "English": "en-us",
    "Japanese": "ja",
    "Chinese": "cmn",
    "French": "fr-fr",
    "German": "de",
}

# Default neutral emotion vector: [happiness, sadness, disgust, fear, surprise, anger, other, neutral]
_DEFAULT_EMOTION = [0.3777, 0.0, 0.0077, 0.0, 0.0537, 0.0, 0.1227, 0.4381]


def _build_emotion_vector(request: TTSRequest) -> list[float]:
    """Build Zonos 8-dim emotion vector from request params.

    Dims: [happiness, sadness, disgust, fear, surprise, anger, other, neutral]
    If emotion_neutral is None, it auto-fills as 1.0 - sum(others), clamped to 0.
    """
    happiness = request.emotion_happiness
    sadness = request.emotion_sadness
    disgust = request.emotion_disgust
    fear = request.emotion_fear
    surprise = request.emotion_surprise
    anger = request.emotion_anger
    other = request.emotion_other

    neutral = max(0.0, 1.0 - (happiness + sadness + disgust + fear + surprise + anger + other))

    return [happiness, sadness, disgust, fear, surprise, anger, other, neutral]


class _ZonosEngine(TTSEngine):
    """Base class for Zonos TTS engines."""

    def __init__(
        self,
        model_id: str,
        device: str = "cuda",
        voices_dir: Path | None = None,
    ):
        from zonos.model import Zonos

        print(f"Loading Zonos model ({model_id})...")
        self._model = Zonos.from_pretrained(model_id, device=device)
        self._model.requires_grad_(False).eval()
        self._device = device
        self._voices_dir = voices_dir
        # Zonos outputs 44.1kHz audio
        self._sample_rate = 44100
        print(f"Zonos loaded! Sample rate: {self._sample_rate}")

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def capabilities(self) -> TTSCapabilities:
        return TTSCapabilities(
            voice_cloning=True,
            voice_design=False,
            preset_voices=[],
            emotion_control=True,
            paralinguistic_tags=False,
            streaming=False,
            languages=SUPPORTED_LANGUAGES,
        )

    def generate(self, request: TTSRequest) -> TTSResult:
        import torchaudio
        from zonos.conditioning import make_cond_dict

        # Resolve voice reference
        speaker = None
        if request.voice and self._voices_dir:
            voice_file = self._voices_dir / f"{request.voice}.wav"
            if voice_file.exists():
                wav, sr = torchaudio.load(str(voice_file))
                speaker = self._model.make_speaker_embedding(wav, sr)

        # Map language name to BCP-47 code
        language = _LANGUAGE_MAP.get(request.language, "en-us")

        cond_kwargs: dict = dict(
            text=request.text,
            language=language,
            speaker=speaker,
            emotion=_build_emotion_vector(request),
        )
        if request.speaking_rate is not None:
            cond_kwargs["speaking_rate"] = request.speaking_rate
        if request.pitch_std is not None:
            cond_kwargs["pitch_std"] = request.pitch_std

        cond_dict = make_cond_dict(**cond_kwargs)
        conditioning = self._model.prepare_conditioning(cond_dict)
        codes = self._model.generate(conditioning)
        wav = self._model.autoencoder.decode(codes).cpu().detach()

        # Normalize to (channels, samples) — autoencoder may return (batch, channels, samples)
        if wav.dim() == 3:
            wav = wav.squeeze(0)  # (1, C, T) -> (C, T)
        elif wav.dim() == 1:
            wav = wav.unsqueeze(0)  # (T,) -> (1, T)

        return TTSResult(audio=wav, sample_rate=self._sample_rate)

    def generate_stream(self, request: TTSRequest) -> Iterator[bytes]:
        raise NotImplementedError("Zonos does not support streaming")

    def unload(self) -> None:
        if hasattr(self, "_model"):
            del self._model
            self._model = None


class ZonosHybridEngine(_ZonosEngine):
    """Zonos v0.1 SSM-Hybrid engine.

    Supports:
    - Zero-shot voice cloning from 10–30s reference audio
    - Fine-grained emotion control (happiness, sadness, anger, fear)
    - Speaking rate and pitch variation
    - 5 languages: English, Japanese, Chinese, French, German
    - <4 GB VRAM

    The hybrid variant uses a Mamba SSM architecture. Requires `mamba-ssm`
    and `causal-conv1d` to be installed in the venv:
        pip install mamba-ssm causal-conv1d --no-build-isolation
    """

    def __init__(self, device: str = "cuda", voices_dir: Path | None = None):
        try:
            import mamba_ssm  # noqa: F401
        except ImportError:
            raise ImportError(
                "Zonos hybrid model requires mamba-ssm. "
                "Install with: pip install mamba-ssm causal-conv1d --no-build-isolation"
            )
        super().__init__(
            model_id="Zyphra/Zonos-v0.1-hybrid",
            device=device,
            voices_dir=voices_dir,
        )

    @property
    def name(self) -> str:
        return "Zonos v0.1 Hybrid"


class ZonosTransformerEngine(_ZonosEngine):
    """Zonos v0.1 Transformer engine.

    Same capabilities as the Hybrid variant but uses a standard Transformer
    architecture instead of the SSM-Hybrid. Slightly higher VRAM usage.
    """

    def __init__(self, device: str = "cuda", voices_dir: Path | None = None):
        super().__init__(
            model_id="Zyphra/Zonos-v0.1-transformer",
            device=device,
            voices_dir=voices_dir,
        )

    @property
    def name(self) -> str:
        return "Zonos v0.1 Transformer"
