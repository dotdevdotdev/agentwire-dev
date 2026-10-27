"""Tests for async run_agentwire_cmd in agentwire/server.py."""

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




