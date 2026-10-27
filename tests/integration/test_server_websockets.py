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




# ---------------------------------------------------------------------------
# /ws/terminal/{name} — interactive PTY (handle_terminal_ws)
# ---------------------------------------------------------------------------
#
# We don't spawn a real tmux process — the goal is to exercise the early
# control flow + the JSON message validation that happens before any PTY
# I/O. The full PTY round-trip is integration territory that needs a live
# tmux server.




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






