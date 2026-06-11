"""Tests for STT server backend selection and the cloud-openai backend.

Targets agentwire.stt.engine (FastAPI-free) so they run without the
[stt] extras installed. No live API calls — urllib is mocked.
"""

import io
import json
import urllib.request

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


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")


class TestBackendSelection:
    def test_cloud_openai_selected_explicitly(self, api_key):
        model, info = engine.load_backend(backend="cloud-openai")
        assert model is None
        assert info["backend"] == "cloud-openai"
        assert info["model"] == engine.OPENAI_DEFAULT_MODEL

    def test_cloud_openai_model_env_override(self, api_key, monkeypatch):
        monkeypatch.setenv("OPENAI_STT_MODEL", "whisper-1")
        _, info = engine.load_backend(backend="cloud-openai")
        assert info["model"] == "whisper-1"

    def test_cloud_openai_without_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            engine.load_backend(backend="cloud-openai")

    def test_key_never_in_model_info(self, api_key):
        _, info = engine.load_backend(backend="cloud-openai")
        assert "sk-test-not-real" not in json.dumps(info)

    def test_auto_prefers_local_over_cloud(self, api_key, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(
            engine, "_load_moonshine", lambda m: (sentinel, {"backend": "moonshine", "model": m})
        )
        model, info = engine.load_backend(backend="auto")
        assert model is sentinel
        assert info["backend"] == "moonshine"

    def test_auto_falls_back_to_cloud_when_locals_unavailable(self, no_local_backends, api_key):
        model, info = engine.load_backend(backend="auto")
        assert model is None
        assert info["backend"] == "cloud-openai"

    def test_auto_falls_back_to_cloud_when_local_load_crashes(self, api_key, monkeypatch):
        # Import succeeds but model load blows up (e.g. corrupt weights, OOM)
        def boom(*args, **kwargs):
            raise RuntimeError("model load failed")

        def importerror(*args, **kwargs):
            raise ImportError("not installed")

        monkeypatch.setattr(engine, "_load_moonshine", boom)
        monkeypatch.setattr(engine, "_load_faster_whisper", boom)
        monkeypatch.setattr(engine, "_load_openai_whisper", importerror)
        model, info = engine.load_backend(backend="auto")
        assert info["backend"] == "cloud-openai"

    def test_auto_without_key_raises_when_locals_unavailable(self, no_local_backends, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="No STT backend available"):
            engine.load_backend(backend="auto")

    def test_forced_moonshine_does_not_fall_back_to_cloud(self, no_local_backends, api_key):
        with pytest.raises(RuntimeError, match="moonshine"):
            engine.load_backend(backend="moonshine")

    def test_forced_whisper_does_not_fall_back_to_cloud(self, no_local_backends, api_key):
        with pytest.raises(RuntimeError, match="Whisper"):
            engine.load_backend(backend="whisper")

    def test_unknown_backend_coerced_to_auto(self, no_local_backends, api_key):
        # e.g. the portal-tier value "custom" leaking into STT_BACKEND
        _, info = engine.load_backend(backend="custom")
        assert info["backend"] == "cloud-openai"


class TestCloudRequest:
    @pytest.fixture
    def wav_file(self, tmp_path):
        path = tmp_path / "utterance.wav"
        path.write_bytes(b"RIFF....WAVEfake-audio-bytes")
        return path

    @pytest.fixture
    def captured(self, monkeypatch):
        """Capture the urllib Request and return a canned OpenAI response."""
        captured = {}

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=None):
            captured["request"] = req
            captured["timeout"] = timeout
            return FakeResponse(json.dumps({"text": " hello world "}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return captured

    def test_request_construction(self, api_key, wav_file, captured):
        result = engine.transcribe_cloud_openai(str(wav_file), "gpt-4o-mini-transcribe")

        req = captured["request"]
        assert req.full_url == "https://api.openai.com/v1/audio/transcriptions"
        assert req.get_method() == "POST"
        assert req.get_header("Authorization") == "Bearer sk-test-not-real"
        assert "multipart/form-data" in req.get_header("Content-type")

        body = req.data
        assert b'name="model"\r\n\r\ngpt-4o-mini-transcribe' in body
        assert b'name="response_format"\r\n\r\njson' in body
        assert b'filename="audio.wav"' in body
        assert b"fake-audio-bytes" in body

        assert result["text"] == "hello world"

    def test_base_url_override(self, api_key, wav_file, captured, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1/")
        engine.transcribe_cloud_openai(str(wav_file), "whisper-1")
        assert captured["request"].full_url == "https://proxy.example.com/v1/audio/transcriptions"

    def test_timeout_env_override(self, api_key, wav_file, captured, monkeypatch):
        monkeypatch.setenv("OPENAI_STT_TIMEOUT", "7")
        engine.transcribe_cloud_openai(str(wav_file), "whisper-1")
        assert captured["timeout"] == 7.0

    def test_no_key_raises_before_any_request(self, wav_file, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        def explode(*args, **kwargs):
            raise AssertionError("network call attempted without key")

        monkeypatch.setattr(urllib.request, "urlopen", explode)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            engine.transcribe_cloud_openai(str(wav_file), "whisper-1")

    def test_transcribe_dispatches_cloud_backend(self, api_key, wav_file, captured):
        info = {"backend": "cloud-openai", "model": "gpt-4o-mini-transcribe"}
        result = engine.transcribe(None, info, str(wav_file))
        assert result["text"] == "hello world"
        assert "transcribe_time" in result
        # The key must never leak into the response payload
        assert "sk-test-not-real" not in json.dumps(result)

    def test_transcribe_without_backend_raises(self):
        with pytest.raises(RuntimeError, match="not loaded"):
            engine.transcribe(None, {}, "/tmp/nope.wav")
