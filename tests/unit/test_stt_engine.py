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

    def test_auto_survives_moonshine_load_crash(self, monkeypatch):
        # Import succeeds but model load blows up (e.g. corrupt weights)
        def boom(*args, **kwargs):
            raise RuntimeError("model load failed")

        sentinel = object()
        monkeypatch.setattr(engine, "_load_moonshine", boom)
        monkeypatch.setattr(
            engine,
            "_load_faster_whisper",
            lambda m, d: (sentinel, {"backend": "faster-whisper", "model": m, "device": d}),
        )
        _, info = engine.load_backend(backend="auto")
        assert info["backend"] == "faster-whisper"

    def test_no_backend_available_raises(self, no_local_backends):
        with pytest.raises(RuntimeError, match="No STT backend available"):
            engine.load_backend(backend="auto")

    def test_forced_moonshine_raises_when_missing(self, no_local_backends):
        with pytest.raises(RuntimeError, match="moonshine"):
            engine.load_backend(backend="moonshine")

    def test_forced_whisper_raises_when_missing(self, no_local_backends):
        with pytest.raises(RuntimeError, match="Whisper"):
            engine.load_backend(backend="whisper")

    def test_unknown_backend_coerced_to_auto(self, monkeypatch):
        # e.g. the portal-tier value "custom" leaking into STT_BACKEND
        sentinel = object()
        monkeypatch.setattr(
            engine, "_load_moonshine", lambda m: (sentinel, {"backend": "moonshine", "model": m})
        )
        _, info = engine.load_backend(backend="custom")
        assert info["backend"] == "moonshine"


class TestEngineConfigFlow:
    """The `stt.engine` tier-orthogonal selector reaches load_backend.

    Regression for the overloaded `stt.backend` (#365): the tier value
    `custom` must never be what the engine sees — the engine reads
    `stt.engine`, so `{backend: custom, engine: whisper}` forces whisper.
    """

    def test_engine_config_forces_whisper(self, monkeypatch):
        from agentwire.config import _dict_to_config

        cfg = _dict_to_config(
            {"stt": {"backend": "custom", "url": "http://shim", "engine": "whisper"}}
        )
        assert cfg.stt.backend == "custom"  # tier — shim boots
        assert cfg.stt.engine == "whisper"  # engine — model selector

        # The engine value (not the tier) drives load_backend.
        sentinel = object()
        monkeypatch.setattr(
            engine,
            "_load_faster_whisper",
            lambda m, d: (sentinel, {"backend": "faster-whisper", "model": m, "device": d}),
        )

        # moonshine must NOT be consulted when whisper is forced
        def fail(*a, **k):
            raise AssertionError("moonshine should not load when engine=whisper")

        monkeypatch.setattr(engine, "_load_moonshine", fail)

        model, info = engine.load_backend(backend=cfg.stt.engine)
        assert model is sentinel
        assert info["backend"] == "faster-whisper"

    def test_engine_defaults_to_auto(self):
        from agentwire.config import _dict_to_config

        cfg = _dict_to_config({"stt": {"backend": "custom", "url": "http://shim"}})
        assert cfg.stt.engine == "auto"

    def test_invalid_engine_rejected(self):
        from agentwire.config import _dict_to_config

        with pytest.raises(ValueError, match="stt.engine"):
            _dict_to_config({"stt": {"engine": "custom"}})


class TestTranscribe:
    def test_transcribe_without_backend_raises(self):
        with pytest.raises(RuntimeError, match="not loaded"):
            engine.transcribe(None, {}, "/tmp/nope.wav")

    def test_transcribe_moonshine_joins_segments(self):
        class FakeMoonshine:
            def transcribe(self, path, model):
                return [" hello ", "world "]

        info = {"backend": "moonshine", "model": "moonshine/base"}
        result = engine.transcribe(FakeMoonshine(), info, "/tmp/audio.wav")
        assert result["text"] == "hello world"
        assert "transcribe_time" in result
