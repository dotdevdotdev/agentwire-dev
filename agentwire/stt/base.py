"""Base class for speech-to-text backends."""

from abc import ABC, abstractmethod
from pathlib import Path


class STTBackend(ABC):
    """Abstract base class for STT backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the backend name."""
        ...

    @abstractmethod
    async def transcribe(self, audio_path: Path) -> str | None:
        """Transcribe audio file to text.

        Args:
            audio_path: Path to audio file (WAV format, 16kHz mono).

        Returns:
            Transcribed text, or None if transcription failed.
        """
        ...


class NoSTT(STTBackend):
    """Sentinel backend for the default tier.

    Speech recognition happens in the browser (Chrome SpeechRecognition);
    the server has no transcription role. ``/transcribe`` returns 501 when
    this backend is active.
    """

    @property
    def name(self) -> str:
        return "none"

    async def transcribe(self, audio_path: Path) -> str | None:
        return None
