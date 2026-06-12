"""Speech-to-text backend for AgentWire."""

import logging
import os
from typing import Any

from .base import NoSTT, STTBackend
from .cloud import DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL, DEFAULT_MODEL, CloudSTTBackend
from .server_backend import STTServerBackend

__all__ = [
    "CloudSTTBackend",
    "NoSTT",
    "STTBackend",
    "STTServerBackend",
    "get_stt_backend",
]

logger = logging.getLogger(__name__)


def get_stt_backend(config: Any) -> STTBackend:
    """Get STT backend based on configuration.

    Three tiers: ``stt.backend: custom`` → HTTP shim at ``stt.url``;
    ``stt.backend: cloud`` → OpenAI-compatible transcription API called
    directly from the portal (settings under ``stt.cloud``, key from the
    env var named by ``stt.cloud.api_key_env``); ``stt.backend: default``
    → NoSTT sentinel (the portal transcribes in the browser, so the
    server has no transcription role).
    """
    stt_config = getattr(config, "stt", None)
    backend = getattr(stt_config, "backend", "default") if stt_config is not None else "default"

    if backend == "custom":
        logger.info(f"Using STT shim at {stt_config.url}")
        return STTServerBackend(
            url=stt_config.url,
            timeout=getattr(stt_config, "timeout", 30),
            instructions=getattr(stt_config, "instructions", ""),
            options=getattr(stt_config, "options", None),
        )

    if backend == "cloud":
        cloud = getattr(stt_config, "cloud", None) or {}
        api_key_env = cloud.get("api_key_env", DEFAULT_API_KEY_ENV)
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(
                f"stt.backend 'cloud' requires the {api_key_env} environment "
                f"variable — add {api_key_env}=... to ~/.agentwire/.env "
                f"(docs/wiki/security/secrets.md; set stt.cloud.api_key_env to "
                f"use a different variable). The key is used server-side only."
            )
        base_url = cloud.get("base_url", DEFAULT_BASE_URL)
        model = cloud.get("model", DEFAULT_MODEL)
        logger.info(f"Using cloud STT: {model} at {base_url}")
        return CloudSTTBackend(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=getattr(stt_config, "timeout", 30),
            language=cloud.get("language", ""),
        )

    logger.info("STT backend: default (browser speech recognition)")
    return NoSTT()
