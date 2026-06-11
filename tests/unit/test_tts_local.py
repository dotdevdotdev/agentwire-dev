"""Tests for the default-tier in-process Kokoro path (#269).

Covers the torch-free audio helper, voice resolution, atomic model download,
the LocalKokoro state machine, the portal's WAV duration parser, and the CLI
tier dispatch. No real model files or network involved.
"""

import subprocess
import sys
import wave
from io import BytesIO
from unittest.mock import MagicMock, patch

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
                KokoroEngine._ensure_file("model.onnx", "http://x/model.onnx")

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
                "model.onnx", "http://x/model.onnx",
                progress_cb=lambda f, d, t: progress_calls.append((f, d, t)),
            )

        assert dest.read_bytes() == b"model-bytes"
        assert not dest.with_suffix(dest.suffix + ".part").exists()
        assert progress_calls == [("model.onnx", 11, 11)]

    def test_cached_file_skips_download(self):
        self.cache_dir.mkdir(parents=True)
        (self.cache_dir / "model.onnx").write_bytes(b"cached")
        with patch("urllib.request.urlretrieve") as mock_dl:
            KokoroEngine._ensure_file("model.onnx", "http://x/model.onnx")
        mock_dl.assert_not_called()

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
        from agentwire.__main__ import _local_say_dispatch
        return _local_say_dispatch("hello", "default", 0.5, 0.5, tts_config)

    def test_default_tier_prefers_kokoro(self):
        with patch("agentwire.__main__._local_say_kokoro", return_value=0) as kokoro, \
             patch("agentwire.__main__._local_say_os") as os_say:
            assert self._dispatch({"backend": "default"}) == 0
        kokoro.assert_called_once()
        os_say.assert_not_called()

    def test_default_tier_falls_back_to_os_voice(self):
        with patch("agentwire.__main__._local_say_kokoro", return_value=1), \
             patch("agentwire.__main__._local_say_os", return_value=0) as os_say:
            assert self._dispatch({"backend": "default"}) == 0
        os_say.assert_called_once()

    def test_backend_none_never_synthesizes(self):
        with patch("agentwire.__main__._local_say_kokoro") as kokoro, \
             patch("agentwire.__main__._local_say_os", return_value=0) as os_say:
            assert self._dispatch({"backend": "none"}) == 0
        kokoro.assert_not_called()
        os_say.assert_called_once()

    def test_custom_tier_uses_shim(self):
        with patch("agentwire.__main__._local_say", return_value=0) as shim, \
             patch("agentwire.__main__._local_say_kokoro") as kokoro, \
             patch("agentwire.__main__._local_say_os") as os_say:
            assert self._dispatch({"backend": "custom"}) == 0
        shim.assert_called_once()
        kokoro.assert_not_called()
        os_say.assert_not_called()


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
