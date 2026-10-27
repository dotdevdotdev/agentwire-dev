"""E2E tests for portal UI rendering and WebSocket protocol.

Uses aiohttp TestClient/TestServer — no real browser required.

Chrome Manual Checklist (when portal is live):
1. Desktop loads with "desktopArea" element
2. Session panel lists active sessions
3. Creating a session triggers WebSocket broadcast
4. Monitor mode shows captured output
5. Terminal mode allows keyboard input
6. Voice panel shows available voices
7. Artifact upload displays in iframe window
8. Window tiling (left/right/grid) works
9. WebSocket reconnects after disconnect
10. Dark mode toggle applies theme variables
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentwire.config import load_config
from agentwire.server import AgentWireServer


@pytest.fixture
async def portal_client(tmp_path):
    """Create portal server with TestClient, all subprocess calls mocked."""
    config = load_config(tmp_path / "nonexistent.yaml")
    config.artifacts = type(config.artifacts)(dir=tmp_path / "artifacts", max_size_mb=10)
    (tmp_path / "artifacts").mkdir()
    server = AgentWireServer(config)
    # Patch run_agentwire_cmd globally so WS handlers never call real subprocesses
    server.run_agentwire_cmd = AsyncMock(return_value=(True, {"sessions": []}))
    async with TestClient(TestServer(server.app)) as client:
        yield client, server


# ---------------------------------------------------------------------------
# Static serving
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStaticServing:

    async def test_index_contains_desktop_area(self, portal_client):
        client, server = portal_client
        with patch.object(server, "_get_voices", new_callable=AsyncMock, return_value=["default"]):
            resp = await client.get("/")
        text = await resp.text()
        assert "desktopArea" in text

    # Static-file routes return 200 if the asset exists, 404 if it doesn't —
    # both are "route wired up correctly". Anything 5xx means the route is
    # broken. CI environments may run without a built frontend, so we don't
    # require 200 specifically.




# ---------------------------------------------------------------------------
# WebSocket protocol
# ---------------------------------------------------------------------------


async def _recv_json(ws, timeout=2.0):
    """Receive a JSON message with timeout to prevent hangs."""
    msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
    return msg


async def _drain_initial(ws):
    """Drain the 2 initial messages (sessions_update + machines_update)."""
    await _recv_json(ws)  # sessions_update
    await _recv_json(ws)  # machines_update


@pytest.mark.e2e
class TestWebSocketProtocol:




    async def test_invalid_json_handled(self, portal_client):
        client, server = portal_client
        async with client.ws_connect("/ws") as ws:
            await _drain_initial(ws)
            await ws.send_str("not valid json {{{")
            # Connection should still be alive
            await ws.send_json({"type": "refresh_sessions"})
            msg = await _recv_json(ws)
            assert msg["type"] == "sessions_update"



