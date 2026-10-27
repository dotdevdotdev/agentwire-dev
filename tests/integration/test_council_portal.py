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



