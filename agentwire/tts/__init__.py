"""AgentWire TTS Module - Multi-backend TTS with hot-swapping support"""

from typing import Any

from .base import TTSCapabilities, TTSEngine, TTSRequest, TTSResult
from .local import kokoro_importable
from .registry import EngineRegistry, registry

__all__ = [
    "TTSCapabilities",
    "TTSEngine",
    "TTSRequest",
    "TTSResult",
    "EngineRegistry",
    "registry",
    "kokoro_importable",
    "DEFAULT_KOKORO_URL",
    "_default_tts_url",
]

# Default-tier Kokoro shim subprocess (tmux ``agentwire-kokoro``). Must agree
# with the shim's ``KOKORO_PORT`` (default 8102) if an operator overrides it.
DEFAULT_KOKORO_URL = "http://localhost:8102"


def _default_tts_url(tts_config: Any) -> str:
    """Resolve the default-tier Kokoro shim URL: ``tts.url`` override or :8102.

    Mirror of ``stt._default_stt_url``. The custom tier uses ``tts.url``
    directly; the default tier falls back to the portal-managed shim port.
    """
    return getattr(tts_config, "url", None) or DEFAULT_KOKORO_URL
