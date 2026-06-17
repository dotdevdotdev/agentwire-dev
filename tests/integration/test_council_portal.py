"""Integration tests for the council board portal surface — the read API and
the watcher delta loop, exercised against a real AgentWireServer app."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentwire.config import load_config
from agentwire.council import inbox, state
from agentwire.server import AgentWireServer

ROSTER = ["brain", "gut", "critic"]
NAME = "proj"


def _make_config(tmp_path):
    config = load_config(tmp_path / "nonexistent.yaml")
    config.artifacts = type(config.artifacts)(dir=tmp_path / "artifacts", max_size_mb=10)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    config.server.auth_token = None
    return config


@pytest.fixture(autouse=True)
def council_root(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "COUNCIL_ROOT", tmp_path / "council")


@pytest.fixture
def sitting():
    s = state.Sitting(
        orchestrator=state.orchestrator_for(NAME),
        roster=ROSTER,
        sessions={lens: state.session_for(NAME, lens) for lens in ROSTER},
        started_at=state.now_iso(),
        next_prompt_id=1,
    )
    state.write_sitting(NAME, s)
    pid = state.allocate_prompt_id(NAME)
    inbox.create_prompt(NAME, pid, "Ship it?", ROSTER)
    return pid


@pytest.fixture
async def client(tmp_path):
    server = AgentWireServer(_make_config(tmp_path))
    async with TestClient(TestServer(server.app)) as c:
        yield c, server


class TestCouncilEndpoints:
    async def test_sittings_lists_live(self, client, sitting):
        c, _ = client
        res = await c.get("/api/council/sittings")
        assert res.status == 200
        body = await res.json()
        assert body["sittings"] == [NAME]

    async def test_live_snapshot(self, client, sitting):
        c, _ = client
        inbox.write_reply(NAME, sitting, "brain", "take", "yes, ship")
        inbox.write_reply(NAME, sitting, "gut", "pass", "")
        res = await c.get("/api/council/live")
        assert res.status == 200
        snap = await res.json()
        assert snap["running"] is True
        assert snap["sitting"] == NAME
        assert snap["roster"] == ROSTER
        assert snap["final"] == 2
        by_soul = {t["soul"]: t for t in snap["tiles"]}
        assert by_soul["brain"]["status"] == "answered"
        assert by_soul["gut"]["status"] == "passed"
        assert by_soul["critic"]["status"] == "pending"

    async def test_live_404_when_no_sitting(self, client):
        c, _ = client
        res = await c.get("/api/council/live?sitting=ghost")
        assert res.status == 404

    async def test_live_history_round(self, client, sitting):
        c, _ = client
        pid2 = state.allocate_prompt_id(NAME)
        inbox.create_prompt(NAME, pid2, "Round 2?", ROSTER)
        # Default → latest round.
        latest = await (await c.get("/api/council/live")).json()
        assert latest["prompt_id"] == pid2
        # Explicit older round.
        old = await (await c.get(f"/api/council/live?prompt_id={sitting}")).json()
        assert old["prompt_id"] == sitting
        assert old["prompt_text"] == "Ship it?"


class TestCouncilWatcher:
    async def test_tick_emits_reset_then_tile(self, client, sitting):
        _, server = client
        sent = []

        async def fake_broadcast(msg_type, data):
            sent.append((msg_type, data))

        server.broadcast_dashboard = fake_broadcast
        server.dashboard_clients.add(object())  # pretend a client is connected

        seen = {}
        # First tick: new round → reset signal, no tiles yet.
        await server._council_tick(NAME, seen, inbox, state,
                                   __import__("agentwire.council.view", fromlist=["x"]))
        assert ("council_update", {"sitting": NAME, "prompt_id": sitting, "reset": True}) in sent

        # A reply lands; next tick emits exactly that soul's tile.
        sent.clear()
        inbox.write_reply(NAME, sitting, "brain", "take", "ship it")
        await server._council_tick(NAME, seen, inbox, state,
                                   __import__("agentwire.council.view", fromlist=["x"]))
        tiles = [d["tile"] for (t, d) in sent if t == "council_update" and "tile" in d]
        assert len(tiles) == 1
        assert tiles[0]["soul"] == "brain"
        assert tiles[0]["status"] == "answered"
        assert tiles[0]["verdict"] == "ship it"

        # No change → no further deltas (the board rests between events).
        sent.clear()
        await server._council_tick(NAME, seen, inbox, state,
                                   __import__("agentwire.council.view", fromlist=["x"]))
        assert sent == []
