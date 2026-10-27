"""Tests for agentwire/council/cli.py — handlers with side effects mocked."""

import argparse
import json

import pytest

from agentwire.council import cli, inbox, state

NAME = "proj"


@pytest.fixture(autouse=True)
def council_root(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "COUNCIL_ROOT", tmp_path / "council")
    # Keep resolution hermetic — no real `git` call for the cwd-slug seed.
    monkeypatch.setattr(state, "default_name", lambda cwd=None: "nomatch")
    return tmp_path / "council"


@pytest.fixture
def mocks(monkeypatch):
    """Mock all session side effects; record calls."""
    calls = {"created": [], "killed": [], "sent": [], "live": set()}
    monkeypatch.setattr(cli, "list_live_sessions", lambda: set(calls["live"]))

    def create(name, roles, posture, model, cwd):
        calls["created"].append((name, roles, posture, model))
        calls["live"].add(name)

    monkeypatch.setattr(cli, "create_session", create)

    def kill(name):
        calls["killed"].append(name)
        calls["live"].discard(name)
        return True

    monkeypatch.setattr(cli, "kill_session", kill)
    monkeypatch.setattr(
        cli, "send_to_session", lambda s, m: calls["sent"].append((s, m))
    )

    def verified(session, message, marker, retries=1):
        calls["sent"].append((session, message))
        return True

    monkeypatch.setattr(cli, "send_verified", verified)
    monkeypatch.setattr(cli, "wait_ready", lambda s, timeout=45.0: True)
    monkeypatch.setattr(cli, "current_session", lambda: None)
    return calls


def _args(**kw):
    ns = argparse.Namespace()
    kw.setdefault("name", NAME)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _start(mocks, roster="brain,gut", **kw):
    args = _args(roster=roster, type=None, model=None, force=False, json=True, **kw)
    return cli.cmd_council_start(args)


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


class TestStart:
    def test_creates_namespaced_sessions_and_sitting(self, mocks, capsys):
        assert _start(mocks) == 0
        payload = _payload(capsys)
        assert payload["success"]
        assert payload["council"] == NAME
        names = [c[0] for c in mocks["created"]]
        assert names == ["agentwire-council-proj", "council-proj-brain", "council-proj-gut"]
        roles = {c[0]: c[1] for c in mocks["created"]}
        assert roles["agentwire-council-proj"] == ["council-orchestrator"]
        assert roles["council-proj-brain"] == ["council-member", "council-brain"]
        sitting = state.read_sitting(NAME)
        assert sitting.roster == ["brain", "gut"]
        assert sitting.sessions == {
            "brain": "council-proj-brain",
            "gut": "council-proj-gut",
        }
        assert sitting.orchestrator == "agentwire-council-proj"




    def test_invalid_name_rejected(self, mocks, capsys):
        assert _start(mocks, name="../evil") == 1







class TestResolution:

    def test_ambiguous_refuses_and_lists(self, mocks, capsys):
        _start(mocks, name="a")
        _start(mocks, name="b")
        capsys.readouterr()
        assert cli.cmd_council_ask(_args(name=None, prompt="x", file=None, json=True)) == 1
        err = _payload(capsys)["error"]
        assert "a" in err and "b" in err and "--name" in err








class TestAsk:
    def test_inbox_before_send_and_fanout(self, mocks, capsys, monkeypatch):
        _start(mocks)
        capsys.readouterr()

        def send_checking(session, message, marker, retries=1):
            # The inbox must exist before any soul could conceivably reply.
            assert inbox.replies_dir(NAME, 1).is_dir()
            mocks["sent"].append((session, message))
            return True

        monkeypatch.setattr(cli, "send_verified", send_checking)
        assert cli.cmd_council_ask(_args(prompt="ship it?", file=None, json=True)) == 0
        payload = _payload(capsys)
        assert payload["prompt_id"] == 1
        assert payload["council"] == NAME
        assert set(payload["sent_to"]) == {"brain", "gut"}
        sessions = [s for s, _ in mocks["sent"]]
        assert set(sessions) == {"council-proj-brain", "council-proj-gut"}
        msg = mocks["sent"][0][1]
        assert "[COUNCIL PROMPT #1]" in msg
        # The fanned reply command carries --name so souls never split sessions.
        assert "council reply --name proj --prompt 1" in msg






class TestCollect:

    def test_complete_round(self, mocks, capsys):
        _start(mocks)
        cli.cmd_council_ask(_args(prompt="x", file=None, json=True))
        inbox.write_reply(NAME, 1, "brain", "take", "yes")
        inbox.write_reply(NAME, 1, "gut", "pass", "")
        capsys.readouterr()
        args = _args(prompt=1, timeout=120, no_wait=False, json=True)
        assert cli.cmd_council_collect(args) == 0
        payload = _payload(capsys)
        assert payload["complete"]
        assert {r["kind"] for r in payload["replies"]} == {"take", "pass"}




