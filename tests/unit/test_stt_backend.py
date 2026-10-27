"""Tests for STT backend selection.

The default tier no longer loads Moonshine in-process — it auto-manages the
standalone shim subprocess and talks to it over HTTP. So both ``default`` and
``custom`` resolve to ``STTServerBackend``; only the URL resolution differs
(default → :8101 unless ``stt.url`` overrides).
"""

from types import SimpleNamespace

from agentwire.stt import NullSTTBackend, STTServerBackend, get_stt_backend


def _cfg(backend: str, url: str | None = None, timeout: int = 30):
    return SimpleNamespace(stt=SimpleNamespace(backend=backend, url=url, timeout=timeout))


class TestGetSttBackend:
    def test_default_tier_returns_managed_shim_at_8101(self):
        backend = get_stt_backend(_cfg("default"))
        assert isinstance(backend, STTServerBackend)
        assert backend.url == "http://localhost:8101"


class TestNoneTier:
    def test_none_tier_returns_null_backend(self):
        backend = get_stt_backend(_cfg("none"))
        assert isinstance(backend, NullSTTBackend)
        assert backend.name == "NullSTT"
