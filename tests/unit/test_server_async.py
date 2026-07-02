"""Tests for async run_agentwire_cmd in agentwire/server.py."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentwire.config import load_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_process(returncode=0, stdout=b"", stderr=b""):
    """Create a mock async subprocess process."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.fixture
def server(tmp_path):
    """Create an AgentWireServer with minimal config."""
    config = load_config(tmp_path / "nonexistent.yaml")
    from agentwire.server import AgentWireServer
    return AgentWireServer(config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAsyncRunAgentwireCmd:
    async def test_success_json(self, server):
        data = {"sessions": [{"name": "test"}]}
        proc = _make_process(stdout=json.dumps(data).encode())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["list", "--sessions"])
        assert success is True
        assert result == data

    async def test_nonzero_returncode(self, server):
        proc = _make_process(returncode=1, stderr=b"session not found")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["kill", "-s", "x"])
        assert success is False
        assert "session not found" in result["error"]

    async def test_json_output_false_success(self, server):
        proc = _make_process(stdout=b"raw output text")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["say", "hello"], json_output=False)
        assert success is True
        assert result["output"] == "raw output text"

    async def test_json_output_false_failure(self, server):
        proc = _make_process(returncode=1, stderr=b"permission denied")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["say", "hi"], json_output=False)
        assert success is False
        assert "permission denied" in result["error"]

    async def test_json_parse_error(self, server):
        proc = _make_process(stdout=b"not valid json")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["info"])
        assert success is False
        assert "Failed to parse" in result["error"]

    async def test_json_flag_inserted_before_dash_dash(self, server):
        """--json must land before a `--` separator, not after it — anything
        after `--` is swallowed into positional args (caught live: the
        first-message text arrived with ` --json` appended)."""
        proc = _make_process(stdout=b'{"success": true}')
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await server.run_agentwire_cmd(
                ["send", "-s", "x", "--wait-ready", "--", "my idea text"])
        cmd_args = list(mock_exec.call_args[0])
        sep = cmd_args.index("--")
        assert "--json" in cmd_args[:sep]
        assert cmd_args[-1] == "my idea text"

    async def test_error_in_stdout_on_failure(self, server):
        error_data = json.dumps({"error": "session locked"}).encode()
        proc = _make_process(returncode=1, stdout=error_data)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["ensure"])
        assert success is False
        assert result["error"] == "session locked"

    async def test_command_construction(self, server):
        proc = _make_process(stdout=b'{"ok": true}')
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await server.run_agentwire_cmd(["new", "-s", "test"])
            cmd_args = mock_exec.call_args[0]
            assert cmd_args == ("agentwire", "new", "-s", "test", "--json")

    async def test_command_no_json_flag(self, server):
        proc = _make_process(stdout=b"ok")
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await server.run_agentwire_cmd(["say", "hi"], json_output=False)
            cmd_args = mock_exec.call_args[0]
            assert "--json" not in cmd_args


class TestServiceAutostartAndWatchdog:
    """Portal-side service lifecycle (#214) — thin shells over the CLI."""

    async def test_autostart_calls_services_up_all(self, server, monkeypatch):
        # Collapse the politeness delay so the test is instant.
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        server.run_agentwire_cmd = AsyncMock(return_value=(True, {"results": [
            {"name": "tracker", "ok": True, "result": "started"},
        ]}))
        await server.autostart_custom_services()
        server.run_agentwire_cmd.assert_awaited_once_with(["services", "up", "--all"])

    async def test_notify_service_event_broadcasts_and_speaks(self, server):
        server.broadcast_dashboard = AsyncMock()
        server.run_agentwire_cmd = AsyncMock(return_value=(True, {}))
        await server._notify_service_event("tracker", "Service tracker is down", speak=True)
        # Toast stored + broadcast
        assert any(n["session"] == "service:tracker"
                   for n in server.active_notifications.values())
        server.broadcast_dashboard.assert_awaited()
        msg_type = server.broadcast_dashboard.await_args[0][0]
        assert msg_type == "notification"
        # TTS via the say CLI
        server.run_agentwire_cmd.assert_awaited_once_with(
            ["say", "Service tracker is down"], json_output=False)

    async def test_notify_replaces_stale_toast_for_same_service(self, server):
        server.broadcast_dashboard = AsyncMock()
        server.run_agentwire_cmd = AsyncMock(return_value=(True, {}))
        await server._notify_service_event("tracker", "down", speak=False)
        await server._notify_service_event("tracker", "recovered", speak=False)
        toasts = [n for n in server.active_notifications.values()
                  if n["session"] == "service:tracker"]
        assert len(toasts) == 1
        assert toasts[0]["text"] == "recovered"


# ---------------------------------------------------------------------------
# TTS contract envelope
# ---------------------------------------------------------------------------


def _mock_tts_post(status=200, body=b"WAVDATA"):
    """Mock _http_session whose .post() is an async context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=cm)
    return session


class TestTtsEnvelope:
    async def test_payload_is_contract_shaped(self, server):
        server.config.tts.backend = "custom"
        server.config.tts.url = "http://localhost:8100"
        server.config.tts.instructions = "speak warmly"
        server.config.tts.options = {"backend": "kokoro"}
        server._http_session = _mock_tts_post()

        audio = await server._tts_generate(
            text="hello",
            voice="amy",
            instructions=server.config.tts.instructions,
            options=server._tts_envelope_options(0.7, 0.3),
        )

        assert audio == b"WAVDATA"
        _, kwargs = server._http_session.post.call_args
        payload = kwargs["json"]
        assert payload["text"] == "hello"
        assert payload["voice"] == "amy"
        assert payload["instructions"] == "speak warmly"
        assert payload["options"] == {
            "exaggeration": 0.7, "cfg_weight": 0.3, "backend": "kokoro",
        }
        # No legacy top-level knobs
        assert "exaggeration" not in payload
        assert "cfg_weight" not in payload

    async def test_minimal_payload_omits_empty_fields(self, server):
        server.config.tts.backend = "custom"
        server.config.tts.url = "http://localhost:8100"
        server._http_session = _mock_tts_post()

        await server._tts_generate(text="hi", voice=None)

        _, kwargs = server._http_session.post.call_args
        payload = kwargs["json"]
        assert payload == {"text": "hi"}

    async def test_config_options_win_over_session_knobs(self, server):
        server.config.tts.options = {"exaggeration": 0.9}
        opts = server._tts_envelope_options(0.5, 0.5)
        assert opts["exaggeration"] == 0.9


# ---------------------------------------------------------------------------
# Default-tier speech path
# ---------------------------------------------------------------------------


def _client(name="c1"):
    """Minimal fake WebSocket client."""
    return MagicMock(name=name)


# Speak fanout reaches session-notify, which PTY-pipes through a real tmux
# binary; deselected in the hermetic CI gate via `-m 'not requires_tmux'` (#323).
@pytest.mark.requires_tmux
class TestSpeakDefaultTier:
    """server.config defaults to tts.backend == 'default' (empty config)."""

    async def test_broadcasts_speak_text_with_tags_stripped(self, server):
        server._broadcast = AsyncMock()
        server.broadcast_dashboard = AsyncMock()
        # Session with a connected client
        from agentwire.server import Session
        session = Session(name="dev", config=await server._get_session_config("dev"))
        session.clients.add(_client())
        server.active_sessions["dev"] = session

        ok = await server.speak("dev", "[laugh] hello <emotion:happy> world")

        assert ok is True
        sent = [c.args[1] for c in server._broadcast.await_args_list]
        speak_msgs = [m for m in sent if m.get("type") == "speak_text"]
        assert speak_msgs == [{"type": "speak_text", "session": "dev", "text": "hello world"}]
        # tts_start still announced
        assert any(m.get("type") == "tts_start" for m in sent)
        server.broadcast_dashboard.assert_any_await("audio_playing", {"session": "dev"})

    async def test_no_clients_falls_back_to_os_voice(self, server):
        server._broadcast = AsyncMock()
        server.broadcast_dashboard = AsyncMock()
        server._os_say = AsyncMock(return_value=True)

        ok = await server.speak("dev", "[sigh] all alone")

        assert ok is True
        server._os_say.assert_awaited_once_with("all alone")
        # No speak_text broadcast happened (no clients)
        sent = [c.args[1] for c in server._broadcast.await_args_list]
        assert not any(m.get("type") == "speak_text" for m in sent)

    async def test_custom_tier_no_clients_returns_false(self, server):
        server.config.tts.backend = "custom"
        server.config.tts.url = "http://localhost:8100"
        server._os_say = AsyncMock()

        ok = await server.speak("dev", "hello")

        assert ok is False
        server._os_say.assert_not_awaited()

    async def test_kokoro_ready_broadcasts_audio_not_speak_text(self, server):
        """Once the managed Kokoro shim is warm (/health ok), the default tier
        sends real WAV audio messages instead of delegating to speechSynthesis."""
        import numpy as np

        from agentwire.tts.audio import pcm_float_to_wav_bytes

        server._broadcast = AsyncMock()
        server.broadcast_dashboard = AsyncMock()
        wav = pcm_float_to_wav_bytes(np.zeros(2400, dtype=np.float32), 24000)
        server._kokoro_shim_ready = AsyncMock(return_value=True)
        server._tts_generate = AsyncMock(return_value=wav)
        from agentwire.server import Session
        session = Session(name="dev", config=await server._get_session_config("dev"))
        session.clients.add(_client())
        server.active_sessions["dev"] = session

        ok = await server.speak("dev", "[laugh] hello world")

        assert ok is True
        sent = [c.args[1] for c in server._broadcast.await_args_list]
        assert any(m.get("type") == "audio" for m in sent)
        assert not any(m.get("type") == "speak_text" for m in sent)
        # Speech tags stripped before synthesis (kokoro has no tag support)
        chunk = server._tts_generate.await_args[0][0]
        assert "[laugh]" not in chunk

    async def test_kokoro_failure_returns_false_no_speak_text(self, server):
        """A ready shim that returns no audio mid-synthesis must not crash speak()."""
        server._broadcast = AsyncMock()
        server.broadcast_dashboard = AsyncMock()
        server._kokoro_shim_ready = AsyncMock(return_value=True)
        server._tts_generate = AsyncMock(return_value=None)
        from agentwire.server import Session
        session = Session(name="dev", config=await server._get_session_config("dev"))
        session.clients.add(_client())
        server.active_sessions["dev"] = session

        ok = await server.speak("dev", "hello")

        assert ok is False

    async def test_notifications_fanout_speaks_text_to_other_sessions(self, server):
        server._broadcast = AsyncMock()
        server.broadcast_dashboard = AsyncMock()
        from agentwire.server import Session
        other = Session(name="dev", config=await server._get_session_config("dev"))
        other.clients.add(_client())
        server.active_sessions["dev"] = other

        ok = await server.speak("agentwire-notifications", "heads up")

        assert ok is True
        # speak_text went to the fan-out target, not the notifications session
        targets = [c.args[0] for c in server._broadcast.await_args_list]
        assert all(t.name == "dev" for t in targets)
