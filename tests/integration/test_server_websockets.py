"""Integration tests for portal WebSocket endpoints.

Covers:

- /ws/{name}              session output stream (handle_websocket)
- /ws/terminal/{name}     interactive PTY (handle_terminal_ws)

Uses aiohttp's TestClient/TestServer harness (same shape as test_portal_ui.py).
The agent backend is replaced with a MagicMock so handle_websocket's
get_output() call is deterministic and doesn't touch tmux.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentwire.config import load_config
from agentwire.server import AgentWireServer

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def portal_client(tmp_path, monkeypatch):
    """Server with mocked subprocess + agent backend, ready for WS connections."""
    config = load_config(tmp_path / "nonexistent.yaml")
    server = AgentWireServer(config)

    # Default agent stub — individual tests can replace .get_output as needed.
    server.agent = MagicMock()
    server.agent.get_output = MagicMock(return_value="initial scrollback")
    server.agent.machines = []

    # Subprocess shouldn't fire from any WS path we test.
    server.run_agentwire_cmd = AsyncMock(return_value=(True, {"sessions": []}))

    async with TestClient(TestServer(server.app)) as client:
        yield client, server


@pytest.fixture
async def portal_client_with_token(tmp_path):
    """Server with bearer-token auth enforced on WS upgrades."""
    config = load_config(tmp_path / "nonexistent.yaml")
    config.server.auth_token = "testtoken123"
    server = AgentWireServer(config)
    server.agent = MagicMock()
    server.agent.get_output = MagicMock(return_value="initial scrollback")
    server.agent.machines = []
    server.run_agentwire_cmd = AsyncMock(return_value=(True, {"sessions": []}))
    async with TestClient(TestServer(server.app)) as client:
        yield client, server


async def _recv_json(ws, timeout=2.0):
    return await asyncio.wait_for(ws.receive_json(), timeout=timeout)


# ---------------------------------------------------------------------------
# /ws/{name} — session output stream (handle_websocket)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSessionWebSocket:
    @pytest.mark.requires_tmux
    async def test_connect_sends_initial_output(self, portal_client):
        client, server = portal_client
        server.agent.get_output.return_value = "hello world"
        async with client.ws_connect("/ws/test-session") as ws:
            msg = await _recv_json(ws)
            assert msg["type"] == "output"
            assert msg["data"] == "hello world"

    @pytest.mark.requires_tmux
    async def test_connect_registers_client(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/sess-a") as ws:
            await _recv_json(ws)  # drain initial output
            assert "sess-a" in server.active_sessions
            assert len(server.active_sessions["sess-a"].clients) == 1

    @pytest.mark.requires_tmux
    async def test_disconnect_removes_client(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/sess-b") as ws:
            await _recv_json(ws)
            assert len(server.active_sessions["sess-b"].clients) == 1
        # After context exit, give the event loop a tick to run the finally block.
        await asyncio.sleep(0.05)
        assert len(server.active_sessions["sess-b"].clients) == 0

    @pytest.mark.requires_tmux
    async def test_multiple_clients_share_session(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/multi") as ws1:
            await _recv_json(ws1)
            async with client.ws_connect("/ws/multi") as ws2:
                await _recv_json(ws2)
                assert len(server.active_sessions["multi"].clients) == 2

    @pytest.mark.requires_tmux
    async def test_recording_started_locks_session(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/locked") as ws1:
            await _recv_json(ws1)
            async with client.ws_connect("/ws/locked") as ws2:
                await _recv_json(ws2)
                await ws1.send_json({"type": "recording_started"})
                # Other client should receive a session_locked notification.
                msg = await _recv_json(ws2)
                assert msg["type"] == "session_locked"
                # Session is now locked by ws1's client_id.
                assert server.active_sessions["locked"].locked_by is not None

    @pytest.mark.requires_tmux
    async def test_lock_released_on_owner_disconnect(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/release") as ws1:
            await _recv_json(ws1)
            await ws1.send_json({"type": "recording_started"})
            await asyncio.sleep(0.05)
            assert server.active_sessions["release"].locked_by is not None
        # Owner disconnected — finally block should clear the lock.
        await asyncio.sleep(0.05)
        assert server.active_sessions["release"].locked_by is None

    @pytest.mark.requires_tmux
    async def test_invalid_json_keeps_connection_alive(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/sess-json") as ws:
            await _recv_json(ws)
            await ws.send_str("garbage }}{{ not json")
            # Still connected — send a valid message and verify nothing crashes.
            await ws.send_json({"type": "resize", "cols": 100, "rows": 30})
            # No reply expected for resize. Confirm session still tracked.
            assert "sess-json" in server.active_sessions

    @pytest.mark.requires_tmux
    async def test_resize_message_accepted(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/sess-resize") as ws:
            await _recv_json(ws)
            # Resize is accepted but produces no broadcast; just verify it doesn't break.
            await ws.send_json({"type": "resize", "cols": 120, "rows": 40})
            await asyncio.sleep(0.05)
            assert ws.closed is False

    async def test_initial_output_failure_does_not_drop_connection(self, portal_client):
        client, server = portal_client
        # Simulate get_output blowing up — connection should still survive.
        server.agent.get_output.side_effect = RuntimeError("tmux gone")
        async with client.ws_connect("/ws/sess-err") as ws:
            # No initial output frame is sent on failure, but the WS stays open.
            # We confirm by sending a no-op message and checking it doesn't error.
            await ws.send_json({"type": "resize", "cols": 80, "rows": 24})
            await asyncio.sleep(0.05)
            assert ws.closed is False


# ---------------------------------------------------------------------------
# /ws/terminal/{name} — interactive PTY (handle_terminal_ws)
# ---------------------------------------------------------------------------
#
# We don't spawn a real tmux process — the goal is to exercise the early
# control flow + the JSON message validation that happens before any PTY
# I/O. The full PTY round-trip is integration territory that needs a live
# tmux server.


@pytest.mark.integration
class TestTerminalWebSocket:
    async def test_remote_machine_not_found_closes_ws(self, portal_client):
        client, server = portal_client
        # session name with @machine triggers remote path; agent has no machines.
        async with client.ws_connect("/ws/terminal/proj@ghost-host") as ws:
            # Server closes immediately when machine config is missing.
            msg = await ws.receive(timeout=2.0)
            # Could be CLOSE or CLOSED — both are acceptable termination signals.
            assert msg.type.name in {"CLOSE", "CLOSED", "CLOSING"}

    @pytest.mark.requires_tmux
    async def test_terminal_registers_client_in_session(self, portal_client):
        client, server = portal_client
        # tmux subprocess spawn will fail in test env; we just verify that
        # the session is registered before the failure path runs.
        async with client.ws_connect("/ws/terminal/local-session") as ws:
            # The handler will fail to spawn tmux and send an error frame.
            # We just need to confirm the session dict was populated.
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
                # Either an error JSON frame or a close — both valid.
                if msg.type.name == "TEXT":
                    payload = json.loads(msg.data)
                    # "local-session" has no machine, so the local PTY path emits the
                    # local_* frames; the remote_* variants are the SSH path's equivalents.
                    assert payload.get("type") in {
                        "error",
                        "local_disconnected",
                        "local_session_ended",
                        "remote_disconnected",
                        "remote_session_ended",
                    }
            except asyncio.TimeoutError:
                pass
        assert "local-session" in server.active_sessions


# ---------------------------------------------------------------------------
# Reconnect + per-session transcript resumption (the #312 failure class)
# ---------------------------------------------------------------------------
#
# #312: the portal dropped to a 'Reconnect' state when an agent killed
# background processes — the tmux child churned, but the *session* and its
# transcript were still alive. These tests pin the server-side contract that
# makes a silent client reconnect possible:
#
#   - the Session object (and its last_output transcript) outlives the last
#     client disconnect, so a returning client resumes the same session;
#   - reconnecting re-sends the *current* scrollback via a fresh get_output,
#     so a client that churned picks up where the transcript actually is
#     rather than starting blank or stuck;
#   - repeated connect/disconnect cycles don't leak client refs.


@pytest.mark.integration
class TestReconnectAndTranscriptResumption:
    async def test_reconnect_resends_transcript(self, portal_client):
        client, server = portal_client
        server.agent.get_output.return_value = "transcript so far"

        # First connection receives the transcript.
        async with client.ws_connect("/ws/resume") as ws1:
            msg = await _recv_json(ws1)
            assert msg["type"] == "output"
            assert msg["data"] == "transcript so far"
        await asyncio.sleep(0.05)  # let the finally block run

        # Reconnect to the SAME session — transcript is re-sent on connect.
        async with client.ws_connect("/ws/resume") as ws2:
            msg = await _recv_json(ws2)
            assert msg["type"] == "output"
            assert msg["data"] == "transcript so far"

    async def test_session_and_transcript_survive_all_clients_disconnect(
        self, portal_client
    ):
        client, server = portal_client
        server.agent.get_output.return_value = "persisted scrollback"
        async with client.ws_connect("/ws/persist") as ws:
            await _recv_json(ws)
            assert server.active_sessions["persist"].last_output == "persisted scrollback"
        await asyncio.sleep(0.05)
        # Last client gone, but the session object and its transcript persist so
        # a reconnect resumes rather than cold-starts.
        assert "persist" in server.active_sessions
        assert len(server.active_sessions["persist"].clients) == 0
        assert server.active_sessions["persist"].last_output == "persisted scrollback"

    async def test_reconnect_reflects_updated_output(self, portal_client):
        """The #312 distinction: a churned child means the transcript MOVED, not
        that the server vanished. Reconnect must surface the newer output."""
        client, server = portal_client
        server.agent.get_output.return_value = "before churn"
        async with client.ws_connect("/ws/churn") as ws1:
            msg = await _recv_json(ws1)
            assert msg["data"] == "before churn"
        await asyncio.sleep(0.05)

        # Underlying tmux output advances while no client is attached.
        server.agent.get_output.return_value = "after churn"
        async with client.ws_connect("/ws/churn") as ws2:
            msg = await _recv_json(ws2)
            assert msg["type"] == "output"
            assert msg["data"] == "after churn"

    async def test_repeated_reconnect_no_client_leak(self, portal_client):
        client, server = portal_client
        for _ in range(4):
            async with client.ws_connect("/ws/cycle") as ws:
                await _recv_json(ws)
                assert len(server.active_sessions["cycle"].clients) == 1
            await asyncio.sleep(0.05)
            assert len(server.active_sessions["cycle"].clients) == 0

    async def test_reconnect_after_lock_owner_left_is_unlocked(self, portal_client):
        """A churned/disconnected lock owner must not strand the session locked —
        the returning client connects to a free session and can re-acquire."""
        client, server = portal_client
        async with client.ws_connect("/ws/relock") as ws1:
            await _recv_json(ws1)
            await ws1.send_json({"type": "recording_started"})
            await asyncio.sleep(0.05)
            assert server.active_sessions["relock"].locked_by is not None
        await asyncio.sleep(0.05)
        assert server.active_sessions["relock"].locked_by is None

        # Reconnect and re-lock cleanly.
        async with client.ws_connect("/ws/relock") as ws2:
            await _recv_json(ws2)
            await ws2.send_json({"type": "recording_started"})
            await asyncio.sleep(0.05)
            assert server.active_sessions["relock"].locked_by is not None

    async def test_distinct_sessions_keep_separate_transcripts(self, portal_client):
        client, server = portal_client
        # get_output is keyed on session name — each session has its own transcript.
        server.agent.get_output.side_effect = (
            lambda name, lines: f"transcript::{name}"
        )
        async with client.ws_connect("/ws/sess-x") as wsx:
            msg_x = await _recv_json(wsx)
            async with client.ws_connect("/ws/sess-y") as wsy:
                msg_y = await _recv_json(wsy)
                assert msg_x["data"] == "transcript::sess-x"
                assert msg_y["data"] == "transcript::sess-y"
                # State stays partitioned per session name.
                assert server.active_sessions["sess-x"].last_output == "transcript::sess-x"
                assert server.active_sessions["sess-y"].last_output == "transcript::sess-y"

    async def test_reconnect_restarts_polling_task(self, portal_client):
        """When the last client leaves, _poll_output exits (its `while clients`
        loop drains). A reconnect must spin up a fresh poll task, not resume a
        dead one — otherwise the resumed session would never stream again."""
        client, server = portal_client
        async with client.ws_connect("/ws/poll") as ws1:
            await _recv_json(ws1)
            first_task = server.active_sessions["poll"].output_task
            assert first_task is not None
        # _poll_output sleeps 0.5s between iterations, so it only re-checks the
        # (now empty) client set at the top of the next loop. Wait it out.
        for _ in range(20):
            if first_task.done():
                break
            await asyncio.sleep(0.1)
        assert first_task.done()

        async with client.ws_connect("/ws/poll") as ws2:
            await _recv_json(ws2)
            second_task = server.active_sessions["poll"].output_task
            assert second_task is not None
            assert second_task is not first_task
            assert not second_task.done()


# ---------------------------------------------------------------------------
# Security: WS upgrade auth (subprotocol bearer) + Origin validation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestWebSocketSecurity:
    async def test_ws_without_token_rejected(self, portal_client_with_token):
        client, _ = portal_client_with_token
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect("/ws")
        assert exc.value.status == 401

    async def test_ws_wrong_token_rejected(self, portal_client_with_token):
        client, _ = portal_client_with_token
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect(
                "/ws/test-session", protocols=("agentwire.bearer.wrong",)
            )
        assert exc.value.status == 401

    @pytest.mark.requires_tmux
    async def test_ws_with_token_connects_and_echoes_protocol(
        self, portal_client_with_token
    ):
        client, _ = portal_client_with_token
        async with client.ws_connect(
            "/ws/test-session", protocols=("agentwire.bearer.testtoken123",)
        ) as ws:
            assert ws.protocol == "agentwire.bearer.testtoken123"
            msg = await _recv_json(ws)
            assert msg["type"] == "output"

    @pytest.mark.requires_tmux
    async def test_ws_bearer_header_also_accepted(self, portal_client_with_token):
        """Non-browser WS clients can use a plain Authorization header."""
        client, _ = portal_client_with_token
        async with client.ws_connect(
            "/ws/test-session",
            headers={"Authorization": "Bearer testtoken123"},
        ) as ws:
            msg = await _recv_json(ws)
            assert msg["type"] == "output"

    async def test_ws_evil_origin_rejected_even_with_token(
        self, portal_client_with_token
    ):
        client, _ = portal_client_with_token
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect(
                "/ws/test-session",
                protocols=("agentwire.bearer.testtoken123",),
                headers={"Origin": "https://evil.example"},
            )
        assert exc.value.status == 403

    @pytest.mark.requires_tmux
    async def test_terminal_ws_with_token_and_query_params(
        self, portal_client_with_token
    ):
        client, server = portal_client_with_token
        # Token via subprotocol coexists with cols/rows query params.
        async with client.ws_connect(
            "/ws/terminal/local-session?cols=80&rows=24",
            protocols=("agentwire.bearer.testtoken123",),
        ) as ws:
            try:
                await asyncio.wait_for(ws.receive(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        assert "local-session" in server.active_sessions

    @pytest.mark.requires_tmux
    async def test_ws_no_token_configured_open(self, portal_client):
        """Loopback default: no token configured, WS connects as before."""
        client, _ = portal_client
        async with client.ws_connect("/ws/test-session") as ws:
            msg = await _recv_json(ws)
            assert msg["type"] == "output"




