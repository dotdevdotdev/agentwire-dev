"""Tests for STT backend selection (two-tier model)."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from agentwire.stt import NoSTT, STTServerBackend, get_stt_backend


def _cfg(backend: str, url: str | None = None, timeout: int = 30):
    return SimpleNamespace(stt=SimpleNamespace(backend=backend, url=url, timeout=timeout))


class TestGetSttBackend:
    def test_default_tier_returns_nostt(self):
        backend = get_stt_backend(_cfg("default"))
        assert isinstance(backend, NoSTT)
        assert backend.name == "none"

    def test_custom_tier_returns_server_backend(self):
        backend = get_stt_backend(_cfg("custom", url="http://localhost:8101", timeout=12))
        assert isinstance(backend, STTServerBackend)
        assert backend.url == "http://localhost:8101"
        assert backend.timeout == 12

    def test_config_without_stt_section_is_default(self):
        backend = get_stt_backend(SimpleNamespace())
        assert isinstance(backend, NoSTT)


class TestNoSTT:
    def test_transcribe_returns_none(self):
        result = asyncio.run(NoSTT().transcribe(Path("/tmp/nonexistent.wav")))
        assert result is None
