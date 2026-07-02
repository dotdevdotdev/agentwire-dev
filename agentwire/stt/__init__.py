"""Speech-to-text backend for AgentWire."""

import logging
import os
from typing import Any

from .base import STTBackend
from .cloud import DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL, DEFAULT_MODEL, CloudSTTBackend
from .local import moonshine_importable
from .server_backend import STTServerBackend

__all__ = [
    "CloudSTTBackend",
    "NullSTTBackend",
    "STTBackend",
    "STTServerBackend",
    "get_stt_backend",
    "moonshine_importable",
]

logger = logging.getLogger(__name__)

DEFAULT_STT_URL = "http://localhost:8101"


class NullSTTBackend(STTBackend):
    """No-op backend for ``stt.backend: none`` (server STT disabled)."""

    @property
    def name(self) -> str:
        return "NullSTT"

    async def transcribe(self, audio_path) -> str | None:
        raise RuntimeError("STT is disabled (stt.backend 'none')")


def _default_stt_url(stt_config: Any) -> str:
    """Resolve the default-tier shim URL: ``stt.url`` override or :8101.

    Must agree with the shim's ``STT_PORT`` (default 8101) if an operator
    overrides either side.
    """
    return getattr(stt_config, "url", None) or DEFAULT_STT_URL


def get_stt_backend(config: Any) -> STTBackend:
    """Get STT backend based on configuration.

    Three resolutions: ``stt.backend: custom`` → HTTP shim at ``stt.url``;
    ``stt.backend: cloud`` → OpenAI-compatible transcription API called
    directly from the portal (settings under ``stt.cloud``, key from the
    env var named by ``stt.cloud.api_key_env``); ``stt.backend: default``
    → the portal-managed Moonshine shim subprocess at ``_default_stt_url``
    (same HTTP client as ``custom``; the portal ensures it's running via
    ``ensure_managed_stt``). Browser SpeechRecognition is the fallback until
    the shim's ``/health`` reports ``ok``.
    """
    stt_config = getattr(config, "stt", None)
    backend = getattr(stt_config, "backend", "default") if stt_config is not None else "default"

    if backend == "none":
        # STT disabled (--no-stt / stt.backend: none). The browser's
        # SpeechRecognition fallback still works client-side; server
        # transcription just isn't available.
        logger.info("STT disabled (backend 'none')")
        return NullSTTBackend()

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

    # default tier → portal-managed shim; custom tier → user/remote-managed
    # shim. Same HTTP client; only the URL resolution and lifecycle differ.
    url = _default_stt_url(stt_config) if backend == "default" else stt_config.url
    logger.info(f"Using STT shim at {url} ({backend} tier)")
    return STTServerBackend(
        url=url,
        timeout=getattr(stt_config, "timeout", 30),
        instructions=getattr(stt_config, "instructions", ""),
        options=getattr(stt_config, "options", None),
    )
