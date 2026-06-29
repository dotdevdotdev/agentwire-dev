"""TTS Engine Base Classes and Abstractions"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

from pydantic import BaseModel

if TYPE_CHECKING:
    import numpy as np
    import torch


@dataclass
class TTSCapabilities:
    """Capabilities supported by a TTS engine."""

    voice_cloning: bool = False
    voice_design: bool = False  # Generate voice from text description
    preset_voices: list[str] = field(default_factory=list)
    emotion_control: bool = False  # instructions parameter support
    paralinguistic_tags: bool = False  # [laugh], [sigh], etc.
    streaming: bool = False
    languages: list[str] = field(default_factory=lambda: ["English"])


class TTSRequest(BaseModel):
    """Request parameters for TTS generation."""

    text: str
    voice: str | None = None  # Voice reference name or preset voice

    # Chatterbox-specific
    exaggeration: float = 0.5
    cfg_weight: float = 0.5

    # Emotion/style instruction (contract envelope field; emotion-capable engines use it)
    instructions: str | None = None
    language: str = "English"

    # Zonos-specific emotion sliders (0.0–1.0, ignored by other backends)
    # Vector order: [happiness, sadness, disgust, fear, surprise, anger, other, neutral]
    # neutral is not exposed — auto-fills as max(0, 1.0 - sum of the 7 below)
    emotion_happiness: float = 0.0
    emotion_sadness: float = 0.0
    emotion_disgust: float = 0.0
    emotion_fear: float = 0.0
    emotion_surprise: float = 0.0
    emotion_anger: float = 0.0
    emotion_other: float = 0.0
    # Speaking characteristics (None = use Zonos defaults)
    speaking_rate: float | None = None  # Tokens/sec (default ~15.0)
    pitch_std: float | None = None  # Pitch variation (default ~45.0)

    # Streaming
    stream: bool = False

    # Backend selection (optional override)
    backend: str | None = None

    # Contract envelope: opaque pass-through. Known keys are folded into the
    # fields above by the server before engine dispatch; unknown keys ignored.
    options: dict | None = None


@dataclass
class TTSResult:
    """Result of TTS generation."""

    audio: "torch.Tensor | np.ndarray"  # Shape: (1, samples); kokoro returns numpy
    sample_rate: int


class TTSEngine(ABC):
    """Abstract base class for TTS backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable engine name."""
        pass

    @property
    @abstractmethod
    def capabilities(self) -> TTSCapabilities:
        """Engine capabilities."""
        pass

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Output sample rate in Hz."""
        pass

    @abstractmethod
    def generate(self, request: TTSRequest) -> TTSResult:
        """Generate audio from request.

        Args:
            request: TTS request parameters

        Returns:
            TTSResult with audio tensor and sample rate
        """
        pass

    def generate_stream(self, request: TTSRequest) -> Iterator[bytes]:
        """Generate audio as a stream of chunks.

        Args:
            request: TTS request parameters

        Yields:
            Audio data chunks as bytes

        Raises:
            NotImplementedError: If streaming not supported
        """
        raise NotImplementedError(f"{self.name} does not support streaming")

    def unload(self) -> None:
        """Release GPU memory and resources.

        Override in subclasses to implement proper cleanup.
        """
        pass
