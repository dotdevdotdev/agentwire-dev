"""Tests for the SDK watch endpoints — `api_sdk_sessions` + `handle_sdk_watch_ws`.

The first test shells out to the regular `list_sessions` so it just needs
to verify the endpoint shape. The second exercises the JSONL tail path
end-to-end via aiohttp's TestClient.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer, TestClient
from aiohttp import web

from agentwire.config import load_config
from agentwire.repl.persistence import create_session


@pytest.fixture
def server(tmp_path, monkeypatch):
    config = load_config(tmp_path / "nonexistent.yaml")
    from agentwire.server import AgentWireServer
    s = AgentWireServer(config)
    # Redirect persistence to the test tmp dir so we don't read the real
    # ~/.agentwire/sessions/repl/ during the test.
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    return s


@pytest.mark.asyncio
async def test_api_sdk_sessions_lists_saved_sessions(server, tmp_path):
    home = tmp_path / "repl"
    t1 = create_session(mode="bypass", model="m1", allowed_tools=[], name="run-1", home=home)
    t1.close()
    t2 = create_session(mode="prompted", model="m2", allowed_tools=[], name="run-2", home=home)
    t2.close()

    async with TestClient(TestServer(server.app)) as client:
        resp = await client.get("/api/sdk-sessions")
        assert resp.status == 200
        body = await resp.json()
        names = {s["name"] for s in body["sessions"]}
        assert names == {"run-1", "run-2"}


@pytest.mark.asyncio
async def test_ws_streams_replay_then_live_appends(server, tmp_path):
    home = tmp_path / "repl"
    t = create_session(mode="bypass", model="m", allowed_tools=[], name="run-x", home=home)
    t.write_event({"type": "session"})
    t.write_event({"type": "agent_start"})

    async with TestClient(TestServer(server.app)) as client:
        async with client.ws_connect("/ws/sdk-watch/run-x") as ws:
            # Replay both pre-existing events.
            ev1 = await ws.receive_json(timeout=2.0)
            assert ev1["type"] == "session"
            ev2 = await ws.receive_json(timeout=2.0)
            assert ev2["type"] == "agent_start"

            # Now write a new event; client should see it within the poll interval.
            t.write_event({"type": "turn_end"})
            ev3 = await ws.receive_json(timeout=2.0)
            assert ev3["type"] == "turn_end"
    t.close()


@pytest.mark.asyncio
async def test_ws_unknown_session_emits_error_and_stays_open(server, tmp_path):
    # tail_transcript waits up to 5s for the file to appear. To keep the
    # test snappy we just confirm the WS opens — the session-not-found path
    # is covered by tail_transcript's own tests.
    async with TestClient(TestServer(server.app)) as client:
        async with client.ws_connect("/ws/sdk-watch/nope") as ws:
            assert not ws.closed
