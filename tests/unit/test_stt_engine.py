"""Tests for STT server (shim) backend selection.

Targets agentwire.stt.engine (FastAPI-free) so they run without the
[stt] extras installed.
"""

import pytest

from agentwire.stt import engine


@pytest.fixture
def no_local_backends(monkeypatch):
    """Make every local backend fail to import, as on a host without [stt] extras."""
    def importerror(*args, **kwargs):
        raise ImportError("not installed")

    monkeypatch.setattr(engine, "_load_moonshine", importerror)
    monkeypatch.setattr(engine, "_load_faster_whisper", importerror)
    monkeypatch.setattr(engine, "_load_openai_whisper", importerror)


class TestBackendSelection:
    def test_auto_prefers_moonshine(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(
            engine, "_load_moonshine", lambda m: (sentinel, {"backend": "moonshine", "model": m})
        )
        model, info = engine.load_backend(backend="auto")
        assert model is sentinel
        assert info["backend"] == "moonshine"

    def test_auto_falls_through_to_faster_whisper(self, monkeypatch):
        def importerror(*args, **kwargs):
            raise ImportError("not installed")

        sentinel = object()
        monkeypatch.setattr(engine, "_load_moonshine", importerror)
        monkeypatch.setattr(
            engine,
            "_load_faster_whisper",
            lambda m, d: (sentinel, {"backend": "faster-whisper", "model": m, "device": d}),
        )
        model, info = engine.load_backend(backend="auto")
        assert model is sentinel
        assert info["backend"] == "faster-whisper"


    def test_no_backend_available_raises(self, no_local_backends):
        with pytest.raises(RuntimeError, match="No STT backend available"):
            engine.load_backend(backend="auto")
