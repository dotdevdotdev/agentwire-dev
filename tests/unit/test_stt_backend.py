"""Tests for STT backend selection.

The default tier no longer loads Moonshine in-process — it auto-manages the
standalone shim subprocess and talks to it over HTTP. So both ``default`` and
``custom`` resolve to ``STTServerBackend``; only the URL resolution differs
(default → :8101 unless ``stt.url`` overrides).
"""

from types import SimpleNamespace

import pytest

from agentwire.stt import NullSTTBackend, STTServerBackend, _default_stt_url, get_stt_backend


def _cfg(backend: str, url: str | None = None, timeout: int = 30):
    return SimpleNamespace(stt=SimpleNamespace(backend=backend, url=url, timeout=timeout))


class TestGetSttBackend:
    def test_default_tier_returns_managed_shim_at_8101(self):
        backend = get_stt_backend(_cfg("default"))
        assert isinstance(backend, STTServerBackend)
        assert backend.url == "http://localhost:8101"

    def test_default_tier_honors_url_override(self):
        backend = get_stt_backend(_cfg("default", url="http://localhost:9999"))
        assert isinstance(backend, STTServerBackend)
        assert backend.url == "http://localhost:9999"

    def test_custom_tier_returns_server_backend(self):
        backend = get_stt_backend(_cfg("custom", url="http://localhost:8101", timeout=12))
        assert isinstance(backend, STTServerBackend)
        assert backend.url == "http://localhost:8101"
        assert backend.timeout == 12

    def test_config_without_stt_section_is_default_shim(self):
        backend = get_stt_backend(SimpleNamespace())
        assert isinstance(backend, STTServerBackend)
        assert backend.url == "http://localhost:8101"


class TestNoneTier:
    def test_none_tier_returns_null_backend(self):
        backend = get_stt_backend(_cfg("none"))
        assert isinstance(backend, NullSTTBackend)
        assert backend.name == "NullSTT"

    def test_none_tier_with_null_url_does_not_crash(self):
        # --no-stt sets backend="none" AND url=None; the factory must not
        # fall through to STTServerBackend(url=None).
        backend = get_stt_backend(_cfg("none", url=None))
        assert isinstance(backend, NullSTTBackend)

    async def test_null_backend_transcribe_raises(self, tmp_path):
        backend = get_stt_backend(_cfg("none"))
        with pytest.raises(RuntimeError, match="disabled"):
            await backend.transcribe(tmp_path / "x.wav")


class TestDefaultSttUrl:
    def test_falls_back_to_8101(self):
        assert _default_stt_url(SimpleNamespace(url=None)) == "http://localhost:8101"

    def test_override_wins(self):
        assert _default_stt_url(SimpleNamespace(url="http://host:1234")) == "http://host:1234"
