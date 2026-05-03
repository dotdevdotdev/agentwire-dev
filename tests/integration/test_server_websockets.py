"""Integration tests for portal WebSocket endpoints.

Covers the 3 endpoints not exercised by tests/unit/test_server_sdk_watch.py
and tests/e2e/test_portal_ui.py:

- /ws/{name}              session output stream (handle_websocket)
- /ws/terminal/{name}     interactive PTY (handle_terminal_ws)
- /ws/sdk-watch/{name}    transcript tail (additional disconnect/replay coverage)

Uses aiohttp's TestClient/TestServer harness (same shape as test_portal_ui.py).
The agent backend is replaced with a MagicMock so handle_websocket's
get_output() call is deterministic and doesn't touch tmux.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentwire.config import load_config
from agentwire.repl.persistence import create_session
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

    # Redirect REPL persistence so /ws/sdk-watch tests are isolated.
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")

    async with TestClient(TestServer(server.app)) as client:
        yield client, server


async def _recv_json(ws, timeout=2.0):
    return await asyncio.wait_for(ws.receive_json(), timeout=timeout)


# ---------------------------------------------------------------------------
# /ws/{name} — session output stream (handle_websocket)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSessionWebSocket:
    async def test_connect_sends_initial_output(self, portal_client):
        client, server = portal_client
        server.agent.get_output.return_value = "hello world"
        async with client.ws_connect("/ws/test-session") as ws:
            msg = await _recv_json(ws)
            assert msg["type"] == "output"
            assert msg["data"] == "hello world"

    async def test_connect_registers_client(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/sess-a") as ws:
            await _recv_json(ws)  # drain initial output
            assert "sess-a" in server.active_sessions
            assert len(server.active_sessions["sess-a"].clients) == 1

    async def test_disconnect_removes_client(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/sess-b") as ws:
            await _recv_json(ws)
            assert len(server.active_sessions["sess-b"].clients) == 1
        # After context exit, give the event loop a tick to run the finally block.
        await asyncio.sleep(0.05)
        assert len(server.active_sessions["sess-b"].clients) == 0

    async def test_multiple_clients_share_session(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/multi") as ws1:
            await _recv_json(ws1)
            async with client.ws_connect("/ws/multi") as ws2:
                await _recv_json(ws2)
                assert len(server.active_sessions["multi"].clients) == 2

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

    async def test_invalid_json_keeps_connection_alive(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws/sess-json") as ws:
            await _recv_json(ws)
            await ws.send_str("garbage }}{{ not json")
            # Still connected — send a valid message and verify nothing crashes.
            await ws.send_json({"type": "resize", "cols": 100, "rows": 30})
            # No reply expected for resize. Confirm session still tracked.
            assert "sess-json" in server.active_sessions

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
                    assert payload.get("type") in {"error", "remote_disconnected", "remote_session_ended"}
            except asyncio.TimeoutError:
                pass
        assert "local-session" in server.active_sessions


# ---------------------------------------------------------------------------
# /ws/sdk-watch/{name} — additional coverage
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSdkWatchWebSocket:
    async def test_disconnect_mid_stream(self, portal_client, tmp_path):
        """Client disconnects while events are being streamed — server should
        cancel the tail task without raising."""
        client, server = portal_client
        home = tmp_path / "repl"
        t = create_session(mode="bypass", model="m", allowed_tools=[], name="mid-stream", home=home)
        try:
            t.write_event({"type": "session"})
            t.write_event({"type": "agent_start"})

            async with client.ws_connect("/ws/sdk-watch/mid-stream") as ws:
                ev1 = await _recv_json(ws)
                assert ev1["type"] == "session"
                # Disconnect after receiving only one of two pre-existing events.
            # Give the cancelled stream task a tick to clean up.
            await asyncio.sleep(0.1)
            # No assertion needed — the goal is to confirm no exceptions leak.
        finally:
            t.close()

    async def test_reconnect_replays_all_events(self, portal_client, tmp_path):
        """Reconnecting to the same session replays the full transcript."""
        client, server = portal_client
        home = tmp_path / "repl"
        t = create_session(mode="bypass", model="m", allowed_tools=[], name="replay", home=home)
        try:
            t.write_event({"type": "session"})
            t.write_event({"type": "agent_start"})
            t.write_event({"type": "turn_end"})

            # First connection — drain all 3 events, disconnect.
            async with client.ws_connect("/ws/sdk-watch/replay") as ws:
                for expected in ("session", "agent_start", "turn_end"):
                    ev = await _recv_json(ws)
                    assert ev["type"] == expected

            await asyncio.sleep(0.05)

            # Second connection — full replay (no dedup on the server side).
            async with client.ws_connect("/ws/sdk-watch/replay") as ws:
                for expected in ("session", "agent_start", "turn_end"):
                    ev = await _recv_json(ws)
                    assert ev["type"] == expected
        finally:
            t.close()

    async def test_inbound_messages_ignored(self, portal_client, tmp_path):
        """Watcher is read-only: inbound text from the client must not break
        the stream."""
        client, server = portal_client
        home = tmp_path / "repl"
        t = create_session(mode="bypass", model="m", allowed_tools=[], name="readonly", home=home)
        try:
            t.write_event({"type": "session"})
            async with client.ws_connect("/ws/sdk-watch/readonly") as ws:
                ev = await _recv_json(ws)
                assert ev["type"] == "session"
                # Send something — should be ignored without error.
                await ws.send_json({"type": "ignored"})
                # Append a new event and confirm it still flows through.
                t.write_event({"type": "turn_end"})
                ev2 = await _recv_json(ws)
                assert ev2["type"] == "turn_end"
        finally:
            t.close()
