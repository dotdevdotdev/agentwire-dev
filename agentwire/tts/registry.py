"""TTS Engine Registry with Hot-Swap Support"""

from pathlib import Path
from typing import Callable

from .base import TTSCapabilities, TTSEngine


def _clear_cuda_cache() -> None:
    """Free CUDA memory after an engine unload. No-op without torch/CUDA —
    the torch-free kokoro path must be able to import this module."""
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()


class EngineRegistry:
    """Registry for TTS engines with hot-swap support.

    Only one engine can be loaded at a time to conserve GPU memory.
    Switching engines automatically unloads the previous one.
    """

    def __init__(self, voices_dir: Path | None = None):
        self._factories: dict[str, Callable[[], TTSEngine]] = {}
        self._current: TTSEngine | None = None
        self._current_name: str | None = None
        self._voices_dir = voices_dir

    def register(self, name: str, factory: Callable[[], TTSEngine]) -> None:
        """Register an engine factory.

        Args:
            name: Unique engine identifier (e.g., "chatterbox", "zonos-hybrid")
            factory: Callable that creates an engine instance
        """
        self._factories[name] = factory

    @property
    def available(self) -> list[str]:
        """List of registered engine names."""
        return list(self._factories.keys())

    @property
    def current_name(self) -> str | None:
        """Name of currently loaded engine."""
        return self._current_name

    @property
    def current(self) -> TTSEngine | None:
        """Currently loaded engine instance."""
        return self._current

    @property
    def current_capabilities(self) -> TTSCapabilities | None:
        """Capabilities of currently loaded engine."""
        return self._current.capabilities if self._current else None

    @property
    def voices_dir(self) -> Path | None:
        """Directory containing voice reference files."""
        return self._voices_dir

    def load(self, name: str) -> TTSEngine:
        """Load an engine, unloading any currently loaded engine.

        Args:
            name: Engine name to load

        Returns:
            Loaded engine instance

        Raises:
            KeyError: If engine name not registered
        """
        if name not in self._factories:
            available = ", ".join(self._factories.keys())
            raise KeyError(f"Unknown engine '{name}'. Available: {available}")

        # Unload current engine if different
        if self._current and self._current_name != name:
            print(f"Unloading {self._current_name}...")
            self._current.unload()
            self._current = None
            self._current_name = None
            _clear_cuda_cache()

        # Load new engine if not already loaded
        if self._current_name != name:
            print(f"Loading {name}...")
            self._current = self._factories[name]()
            self._current_name = name
            print(f"Loaded {name} ({self._current.name})")

        return self._current

    def get_or_load(self, name: str) -> TTSEngine:
        """Get current engine if matching, otherwise load.

        Args:
            name: Engine name

        Returns:
            Engine instance
        """
        if self._current_name == name:
            return self._current
        return self.load(name)

    def unload_current(self) -> None:
        """Unload the currently loaded engine."""
        if self._current:
            print(f"Unloading {self._current_name}...")
            self._current.unload()
            self._current = None
            self._current_name = None
            _clear_cuda_cache()

    def get_voice_path(self, voice_name: str) -> Path | None:
        """Get path to voice reference file.

        Args:
            voice_name: Voice name (without extension)

        Returns:
            Path to voice file, or None if not found
        """
        if not self._voices_dir:
            return None
        voice_path = self._voices_dir / f"{voice_name}.wav"
        return voice_path if voice_path.exists() else None


# Global registry instance
registry = EngineRegistry()
