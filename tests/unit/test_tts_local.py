"""Tests for the default-tier in-process Kokoro path (#269).

Covers the torch-free audio helper, voice resolution, atomic model download,
the LocalKokoro state machine, the portal's WAV duration parser, and the CLI
tier dispatch. No real model files or network involved.
"""

import importlib.util
import subprocess
import sys
import wave
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from agentwire.tts.audio import pcm_float_to_wav_bytes
from agentwire.tts.engines.kokoro import (
    DEFAULT_VOICE,
    PRESET_VOICES,
    KokoroEngine,
    resolve_voice_name,
)
from agentwire.tts.local import LocalKokoro

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

    def test_squeezes_2d_input(self):
        samples = np.zeros((1, 1000), dtype=np.float32)
        wav = pcm_float_to_wav_bytes(samples, 24000)
        with wave.open(BytesIO(wav)) as wf:
            assert wf.getnframes() == 1000

    def test_clips_out_of_range(self):
        samples = np.array([2.0, -2.0], dtype=np.float32)
        wav = pcm_float_to_wav_bytes(samples, 24000)
        with wave.open(BytesIO(wav)) as wf:
            frames = np.frombuffer(wf.readframes(2), dtype=np.int16)
        assert frames[0] == 32767 and frames[1] == -32768


# ---------------------------------------------------------------------------
# Voice resolution
# ---------------------------------------------------------------------------


class TestResolveVoiceName:
    def test_known_preset_passes_through(self):
        assert resolve_voice_name("af_bella") == "af_bella"

    def test_unknown_falls_back_to_default(self):
        assert resolve_voice_name("dotdev") == DEFAULT_VOICE
        assert resolve_voice_name("default") == DEFAULT_VOICE
        assert resolve_voice_name(None) == DEFAULT_VOICE

    def test_random_picks_a_preset(self):
        assert resolve_voice_name("random") in PRESET_VOICES


# ---------------------------------------------------------------------------
# Atomic model download
# ---------------------------------------------------------------------------


import hashlib

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

    def test_successful_download_renamed_atomically(self):
        def ok(url, dest, reporthook=None):
            from pathlib import Path
            Path(dest).write_bytes(b"model-bytes")
            if reporthook:
                reporthook(1, 11, 11)

        progress_calls = []
        with patch("urllib.request.urlretrieve", side_effect=ok):
            dest = KokoroEngine._ensure_file(
                "model.onnx", "http://x/model.onnx", GOOD_SHA,
                progress_cb=lambda f, d, t: progress_calls.append((f, d, t)),
            )

        assert dest.read_bytes() == b"model-bytes"
        assert not dest.with_suffix(dest.suffix + ".part").exists()
        assert progress_calls == [("model.onnx", 11, 11)]

    def test_cached_file_skips_download(self):
        self.cache_dir.mkdir(parents=True)
        (self.cache_dir / "model.onnx").write_bytes(b"cached")
        with patch("urllib.request.urlretrieve") as mock_dl:
            KokoroEngine._ensure_file("model.onnx", "http://x/model.onnx", GOOD_SHA)
        mock_dl.assert_not_called()

    def test_sha256_mismatch_rejected_and_no_file_left(self):
        def ok(url, dest, reporthook=None):
            from pathlib import Path
            Path(dest).write_bytes(b"tampered-bytes")

        with patch("urllib.request.urlretrieve", side_effect=ok):
            with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
                KokoroEngine._ensure_file("model.onnx", "http://x/model.onnx", GOOD_SHA)

        assert not (self.cache_dir / "model.onnx").exists()
        assert not (self.cache_dir / "model.onnx.part").exists()

    def test_sha256_match_accepted(self):
        def ok(url, dest, reporthook=None):
            from pathlib import Path
            Path(dest).write_bytes(b"model-bytes")

        with patch("urllib.request.urlretrieve", side_effect=ok):
            dest = KokoroEngine._ensure_file("model.onnx", "http://x/model.onnx", GOOD_SHA)

        assert dest.read_bytes() == b"model-bytes"

    def test_model_files_cached(self):
        assert KokoroEngine.model_files_cached() is False
        self.cache_dir.mkdir(parents=True)
        (self.cache_dir / KokoroEngine._MODEL_FILE).write_bytes(b"m")
        (self.cache_dir / KokoroEngine._VOICES_FILE).write_bytes(b"v")
        assert KokoroEngine.model_files_cached() is True


# ---------------------------------------------------------------------------
# LocalKokoro state machine
# ---------------------------------------------------------------------------


def _fake_engine_cls(cached=True, download_error=None):
    """Stand-in for KokoroEngine: no network, no onnx."""
    fake = MagicMock()
    fake.model_files_cached.return_value = cached
    if download_error:
        fake.download_models.side_effect = download_error
    fake.return_value = MagicMock(name="engine-instance")
    return fake


class TestLocalKokoro:
    async def test_warm_up_with_cached_model_reaches_ready(self):
        manager = LocalKokoro()
        states = []

        async def on_change(m):
            states.append(m.state)

        with patch("agentwire.tts.engines.kokoro.KokoroEngine", _fake_engine_cls()):
            manager.start(on_change)
            await manager._task

        assert manager.ready
        assert states == ["loading", "ready"]
        assert manager.percent == 100

    async def test_download_failure_reaches_failed(self):
        manager = LocalKokoro()
        fake = _fake_engine_cls(cached=False, download_error=OSError("network down"))
        with patch("agentwire.tts.engines.kokoro.KokoroEngine", fake):
            manager.start()
            await manager._task

        assert manager.state == "failed"
        assert "network down" in manager.error
        assert not manager.ready

    async def test_not_importable_is_terminal_unavailable(self):
        manager = LocalKokoro()
        with patch("agentwire.tts.local.kokoro_importable", return_value=False):
            manager.start()

        assert manager.state == "unavailable"
        assert manager._task is None

    async def test_start_is_idempotent(self):
        manager = LocalKokoro()
        with patch("agentwire.tts.engines.kokoro.KokoroEngine", _fake_engine_cls()):
            manager.start()
            task = manager._task
            manager.start()
            assert manager._task is task
            await manager._task

    async def test_synthesize_raises_until_ready(self):
        manager = LocalKokoro()
        with pytest.raises(RuntimeError, match="not ready"):
            await manager.synthesize("hello")

    async def test_synthesize_returns_wav_and_duration(self):
        manager = LocalKokoro()
        manager.state = "ready"
        result = MagicMock()
        result.audio = np.zeros((1, 12000), dtype=np.float32)
        result.sample_rate = 24000
        manager._engine = MagicMock()
        manager._engine.generate.return_value = result

        wav, duration = await manager.synthesize("hello", "af_bella")

        assert wav[:4] == b"RIFF"
        assert duration == pytest.approx(0.5)
        request = manager._engine.generate.call_args[0][0]
        assert request.voice == "af_bella"

    async def test_close_cancels_warm_up(self):
        manager = LocalKokoro()
        fake = _fake_engine_cls(cached=False)
        # Short sleep: the to_thread worker can't be interrupted, only the
        # awaiting task — keep it brief so executor shutdown doesn't drag.
        fake.download_models.side_effect = lambda cb=None: __import__("time").sleep(1)
        with patch("agentwire.tts.engines.kokoro.KokoroEngine", fake):
            manager.start()
            await manager.close()
        assert manager._task is None


# ---------------------------------------------------------------------------
# Portal WAV duration parser
# ---------------------------------------------------------------------------


class TestWavDurationSeconds:
    def test_exact_duration_from_header(self):
        from agentwire.server import AgentWireServer
        wav = pcm_float_to_wav_bytes(np.zeros(36000, dtype=np.float32), 24000)
        assert AgentWireServer._wav_duration_seconds(wav) == pytest.approx(1.5)

    def test_garbage_returns_none(self):
        from agentwire.server import AgentWireServer
        assert AgentWireServer._wav_duration_seconds(b"not a wav") is None
        assert AgentWireServer._wav_duration_seconds(b"") is None


# ---------------------------------------------------------------------------
# CLI tier dispatch
# ---------------------------------------------------------------------------


class TestLocalSayDispatch:
    def _dispatch(self, tts_config):
        from agentwire.channels_cli import _local_say_dispatch
        return _local_say_dispatch("hello", "default", 0.5, 0.5, tts_config)

    def test_default_tier_prefers_kokoro(self):
        with patch("agentwire.channels_cli._local_say_kokoro", return_value=0) as kokoro, \
             patch("agentwire.channels_cli._local_say_os") as os_say:
            # Returns (return_code, sink) — the sink names the path that played.
            assert self._dispatch({"backend": "default"}) == (0, "local-speakers (kokoro)")
        kokoro.assert_called_once()
        os_say.assert_not_called()

    def test_default_tier_falls_back_to_os_voice(self):
        with patch("agentwire.channels_cli._local_say_kokoro", return_value=1), \
             patch("agentwire.channels_cli._local_say_os", return_value=0) as os_say:
            assert self._dispatch({"backend": "default"}) == (0, "os-voice")
        os_say.assert_called_once()

    def test_backend_none_never_synthesizes(self):
        with patch("agentwire.channels_cli._local_say_kokoro") as kokoro, \
             patch("agentwire.channels_cli._local_say_os", return_value=0) as os_say:
            assert self._dispatch({"backend": "none"}) == (0, "os-voice")
        kokoro.assert_not_called()
        os_say.assert_called_once()

    def test_custom_tier_uses_shim(self):
        with patch("agentwire.channels_cli._local_say", return_value=0) as shim, \
             patch("agentwire.channels_cli._local_say_kokoro") as kokoro, \
             patch("agentwire.channels_cli._local_say_os") as os_say:
            assert self._dispatch({"backend": "custom"}) == (0, "custom-server")
        shim.assert_called_once()
        kokoro.assert_not_called()
        os_say.assert_not_called()


# ---------------------------------------------------------------------------
# Process-isolated Kokoro shim server (kokoro_server.py)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("fastapi") is None,
    reason="fastapi only ships in the stt/tts extras (the shim runs from the source venv)",
)
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

    def test_health_reports_loading_state(self):
        fake = self._fake_kokoro(state="downloading", percent=42)
        with self._client(fake) as c:
            body = c.get("/health").json()
        assert body == {"status": "downloading", "engine": "kokoro", "percent": 42}

    def test_health_ok_when_ready(self):
        fake = self._fake_kokoro(ready=True, state="ready", percent=100)
        with self._client(fake) as c:
            body = c.get("/health").json()
        assert body["status"] == "ok"
        assert body["percent"] == 100

    def test_health_surfaces_error(self):
        fake = self._fake_kokoro(state="failed", error="network down")
        with self._client(fake) as c:
            body = c.get("/health").json()
        assert body["status"] == "failed"
        assert body["error"] == "network down"

    def test_tts_503_until_ready(self):
        fake = self._fake_kokoro(ready=False, state="loading")
        with self._client(fake) as c:
            resp = c.post("/tts", json={"text": "hello"})
        assert resp.status_code == 503

    def test_tts_400_on_empty_text(self):
        fake = self._fake_kokoro(ready=True)
        with self._client(fake) as c:
            resp = c.post("/tts", json={"text": "   "})
        assert resp.status_code == 400

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

    def test_voices_empty_until_ready_then_presets(self):
        with self._client(self._fake_kokoro(ready=False)) as c:
            assert c.get("/voices").json() == {"voices": []}
        with self._client(self._fake_kokoro(ready=True)) as c:
            assert c.get("/voices").json()["voices"] == list(PRESET_VOICES)

    def test_capabilities_shape(self):
        with self._client(self._fake_kokoro(ready=True)) as c:
            caps = c.get("/capabilities").json()
        assert caps["engine"] == "kokoro"
        assert caps["emotion_control"] is False
        assert caps["voice_cloning"] is False
        assert caps["voices"] == list(PRESET_VOICES)


# ---------------------------------------------------------------------------
# Torch-free import guarantee
# ---------------------------------------------------------------------------


class TestTorchFreeImport:
    def test_kokoro_import_pulls_no_torch_or_sibling_engines(self):
        code = (
            "import sys; "
            "import agentwire.tts.engines.kokoro; "
            "import agentwire.tts.local; "
            "assert 'torch' not in sys.modules, 'torch leaked'; "
            "assert 'agentwire.tts.engines.chatterbox' not in sys.modules, "
            "'chatterbox eagerly imported'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
