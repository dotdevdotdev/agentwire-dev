"""Speech-to-text backend for AgentWire."""

import logging
from typing import Any

from .base import NoSTT, STTBackend
from .server_backend import STTServerBackend

__all__ = [
    "NoSTT",
    "STTBackend",
    "STTServerBackend",
    "get_stt_backend",
]

logger = logging.getLogger(__name__)


def get_stt_backend(config: Any) -> STTBackend:
    """Get STT backend based on configuration.

    Two tiers: ``stt.backend: custom`` → HTTP shim at ``stt.url``;
    ``stt.backend: default`` → NoSTT sentinel (the portal transcribes in the
    browser, so the server has no transcription role).
    """
    stt_config = getattr(config, "stt", None)
    if stt_config is not None and getattr(stt_config, "backend", "default") == "custom":
        logger.info(f"Using STT shim at {stt_config.url}")
        return STTServerBackend(url=stt_config.url, timeout=getattr(stt_config, "timeout", 30))
    logger.info("STT backend: default (browser speech recognition)")
    return NoSTT()
