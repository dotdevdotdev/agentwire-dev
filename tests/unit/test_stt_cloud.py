"""Tests for the cloud STT tier (portal → OpenAI-compatible transcription API).

No live API calls — urllib is mocked.
"""

import asyncio
import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from agentwire.stt import CloudSTTBackend, NoSTT, get_stt_backend
from agentwire.stt import cloud as cloud_module


def _cfg(backend: str, cloud: dict | None = None, timeout: int = 30):
    return SimpleNamespace(
        stt=SimpleNamespace(backend=backend, url=None, timeout=timeout, cloud=cloud or {})
    )


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")


class TestGetSttBackendCloudTier:
    def test_cloud_tier_returns_cloud_backend_with_defaults(self, api_key):
        backend = get_stt_backend(_cfg("cloud"))
        assert isinstance(backend, CloudSTTBackend)
        assert backend.name == "cloud"
        assert backend.base_url == cloud_module.DEFAULT_BASE_URL
        assert backend.model == cloud_module.DEFAULT_MODEL

    def test_cloud_tier_reads_cloud_config(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        backend = get_stt_backend(
            _cfg(
                "cloud",
                cloud={
                    "base_url": "https://api.groq.com/openai/v1",
                    "model": "whisper-large-v3-turbo",
                    "api_key_env": "GROQ_API_KEY",
                    "language": "en",
                },
                timeout=12,
            )
        )
        assert backend.base_url == "https://api.groq.com/openai/v1"
        assert backend.model == "whisper-large-v3-turbo"
        assert backend.timeout == 12
        assert backend.language == "en"

    def test_cloud_tier_without_key_fails_fast(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            get_stt_backend(_cfg("cloud"))

    def test_cloud_tier_missing_custom_env_names_it(self, monkeypatch):
        monkeypatch.delenv("MY_STT_KEY", raising=False)
        with pytest.raises(ValueError, match="MY_STT_KEY"):
            get_stt_backend(_cfg("cloud", cloud={"api_key_env": "MY_STT_KEY"}))

    def test_default_tier_still_nostt(self):
        assert isinstance(get_stt_backend(_cfg("default")), NoSTT)


class TestCloudRequest:
    @pytest.fixture
    def wav_file(self, tmp_path):
        path = tmp_path / "utterance.wav"
        path.write_bytes(b"RIFF....WAVEfake-audio-bytes")
        return path

    @pytest.fixture
    def captured(self, monkeypatch):
        """Capture the urllib Request and return a canned provider response."""
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

    def test_request_construction(self, wav_file, captured):
        backend = CloudSTTBackend(api_key="sk-test-not-real")
        text = asyncio.run(backend.transcribe(wav_file))

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
        # No language hint unless configured
        assert b'name="language"' not in body

        assert text == "hello world"

    def test_language_hint_included_when_set(self, wav_file, captured):
        backend = CloudSTTBackend(api_key="sk-test-not-real", language="en")
        asyncio.run(backend.transcribe(wav_file))
        assert b'name="language"\r\n\r\nen' in captured["request"].data

    def test_base_url_trailing_slash_normalized(self, wav_file, captured):
        backend = CloudSTTBackend(
            api_key="gsk-test", base_url="https://api.groq.com/openai/v1/", model="whisper-1"
        )
        asyncio.run(backend.transcribe(wav_file))
        req = captured["request"]
        assert req.full_url == "https://api.groq.com/openai/v1/audio/transcriptions"
        assert b'name="model"\r\n\r\nwhisper-1' in req.data

    def test_timeout_passed_through(self, wav_file, captured):
        backend = CloudSTTBackend(api_key="sk-test-not-real", timeout=7)
        asyncio.run(backend.transcribe(wav_file))
        assert captured["timeout"] == 7

    def test_empty_key_rejected_at_construction(self):
        with pytest.raises(ValueError, match="API key"):
            CloudSTTBackend(api_key="")

    def test_network_error_wrapped_without_key(self, wav_file, monkeypatch):
        def fail(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fail)
        backend = CloudSTTBackend(api_key="sk-test-not-real")
        with pytest.raises(RuntimeError, match="Cloud STT error") as exc_info:
            asyncio.run(backend.transcribe(wav_file))
        assert "sk-test-not-real" not in str(exc_info.value)
