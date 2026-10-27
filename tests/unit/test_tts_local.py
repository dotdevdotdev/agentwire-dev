"""Tests for the default-tier in-process Kokoro path (#269).

Covers the torch-free audio helper, voice resolution, atomic model download,
the LocalKokoro state machine, the portal's WAV duration parser, and the CLI
tier dispatch. No real model files or network involved.
"""

import hashlib
import wave
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from agentwire.tts.audio import pcm_float_to_wav_bytes
from agentwire.tts.engines.kokoro import (
    KokoroEngine,
)

# ---------------------------------------------------------------------------
# pcm_float_to_wav_bytes
# ---------------------------------------------------------------------------


class TestPcmFloatToWavBytes:
    def test_produces_valid_mono_16bit_wav(self):
        samples = np.sin(np.linspace(0, 100, 24000, dtype=np.float32))
        wav = pcm_float_to_wav_bytes(samples, 24000)
        assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
        with wave.open(BytesIO(wav)) as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 24000
            assert wf.getnframes() == 24000


# ---------------------------------------------------------------------------
# Voice resolution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Atomic model download
# ---------------------------------------------------------------------------


# SHA-256 of the b"model-bytes" fixture used throughout TestEnsureFile
GOOD_SHA = hashlib.sha256(b"model-bytes").hexdigest()


class TestEnsureFile:
    @pytest.fixture(autouse=True)
    def fake_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        self.cache_dir = tmp_path / ".cache" / "kokoro_onnx"

    def test_interrupted_download_leaves_no_file(self):
        def boom(url, dest, reporthook=None):
            (self.cache_dir / "model.onnx.part").write_bytes(b"trunc")
            raise OSError("connection reset")

        with patch("urllib.request.urlretrieve", side_effect=boom):
            with pytest.raises(OSError):
                KokoroEngine._ensure_file("model.onnx", "http://x/model.onnx", GOOD_SHA)

        assert not (self.cache_dir / "model.onnx").exists()
        assert not (self.cache_dir / "model.onnx.part").exists()


    def test_sha256_mismatch_rejected_and_no_file_left(self):
        def ok(url, dest, reporthook=None):
            from pathlib import Path
            Path(dest).write_bytes(b"tampered-bytes")

        with patch("urllib.request.urlretrieve", side_effect=ok):
            with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
                KokoroEngine._ensure_file("model.onnx", "http://x/model.onnx", GOOD_SHA)

        assert not (self.cache_dir / "model.onnx").exists()
        assert not (self.cache_dir / "model.onnx.part").exists()


# ---------------------------------------------------------------------------
# LocalKokoro state machine
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Portal WAV duration parser
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI tier dispatch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Process-isolated Kokoro shim server (kokoro_server.py)
# ---------------------------------------------------------------------------


class TestKokoroServer:
    """The shim wraps one LocalKokoro and serves the contract envelope.

    The portal talks to this over HTTP so the GIL-holding warm-up runs in a
    child process, never on the portal event loop (#398)."""

    @contextmanager
    def _client(self, fake):
        from fastapi.testclient import TestClient

        from agentwire.tts import kokoro_server

        # Replace the module-level engine with a controllable fake for the whole
        # test (the patch must outlive lifespan startup, which calls start(),
        # and every request, which reads the module global). start()/close()
        # become no-ops on the fake — no real ONNX load.
        with patch.object(kokoro_server, "kokoro", fake):
            with TestClient(kokoro_server.app) as client:
                yield client

    def _fake_kokoro(self, *, ready=False, state="loading", percent=0, error=None):
        fake = MagicMock()
        fake.ready = ready
        fake.state = state
        fake.percent = percent
        fake.error = error
        fake.start = MagicMock()
        fake.close = AsyncMock()
        return fake


    def test_tts_503_until_ready(self):
        fake = self._fake_kokoro(ready=False, state="loading")
        with self._client(fake) as c:
            resp = c.post("/tts", json={"text": "hello"})
        assert resp.status_code == 503


    def test_tts_returns_wav_when_ready(self):
        wav = pcm_float_to_wav_bytes(np.zeros(2400, dtype=np.float32), 24000)
        fake = self._fake_kokoro(ready=True)
        fake.synthesize = AsyncMock(return_value=(wav, 0.1))
        with self._client(fake) as c:
            resp = c.post("/tts", json={"text": "hello", "voice": "af_bella"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content[:4] == b"RIFF"
        # Voice passed through to the engine.
        assert fake.synthesize.await_args[0] == ("hello", "af_bella")


# ---------------------------------------------------------------------------
# Torch-free import guarantee
# ---------------------------------------------------------------------------
