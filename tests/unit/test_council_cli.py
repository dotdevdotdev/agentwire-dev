"""Tests for agentwire/council/cli.py — handlers with side effects mocked."""

import argparse
import json

import pytest

from agentwire.council import cli, inbox, state


@pytest.fixture(autouse=True)
def council_dirs(tmp_path, monkeypatch):
    council = tmp_path / "council"
    monkeypatch.setattr(state, "COUNCIL_DIR", council)
    monkeypatch.setattr(state, "SITTING_PATH", council / "sitting.json")
    monkeypatch.setattr(state, "WORKSPACE_DIR", council / "workspace")
    monkeypatch.setattr(state, "PROMPTS_DIR", council / "prompts")
    return council


@pytest.fixture
def mocks(monkeypatch):
    """Mock all session side effects; record calls."""
    calls = {"created": [], "killed": [], "sent": [], "live": set()}
    monkeypatch.setattr(cli, "list_live_sessions", lambda: set(calls["live"]))

    def create(name, roles, session_type, model):
        calls["created"].append((name, roles, session_type, model))
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
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _start(mocks, roster="brain,gut", **kw):
    args = _args(roster=roster, type=None, model=None, force=False, json=True, **kw)
    return cli.cmd_council_start(args)


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


class TestStart:
    def test_creates_sessions_and_sitting(self, mocks, capsys):
        assert _start(mocks) == 0
        payload = _payload(capsys)
        assert payload["success"]
        names = [c[0] for c in mocks["created"]]
        assert names == ["agentwire-council", "council-brain", "council-gut"]
        roles = {c[0]: c[1] for c in mocks["created"]}
        assert roles["agentwire-council"] == ["council-orchestrator"]
        assert roles["council-brain"] == ["council-member", "council-brain"]
        sitting = state.read_sitting()
        assert sitting.roster == ["brain", "gut"]
        assert sitting.sessions == {"brain": "council-brain", "gut": "council-gut"}

    def test_writes_workspace_config(self, mocks, capsys):
        _start(mocks)
        yml = (state.WORKSPACE_DIR / ".agentwire.yml").read_text()
        assert "parent: agentwire-council" in yml

    def test_default_roster(self, mocks, capsys):
        _start(mocks, roster=None)
        assert state.read_sitting().roster == state.DEFAULT_ROSTER

    def test_invalid_lens_rejected(self, mocks, capsys):
        assert _start(mocks, roster="brain,../etc") == 1
        assert state.read_sitting() is None

    def test_refuses_live_sitting(self, mocks, capsys):
        _start(mocks)
        capsys.readouterr()
        assert _start(mocks) == 1
        assert not _payload(capsys)["success"]

    def test_force_restarts(self, mocks, capsys):
        _start(mocks)
        capsys.readouterr()
        args = _args(roster="brain", type=None, model=None, force=True, json=True)
        assert cli.cmd_council_start(args) == 0
        assert "agentwire-council" in mocks["killed"]
        assert state.read_sitting().roster == ["brain"]


class TestStop:
    def test_kills_and_clears(self, mocks, capsys):
        _start(mocks)
        capsys.readouterr()
        assert cli.cmd_council_stop(_args(json=True)) == 0
        payload = _payload(capsys)
        assert set(payload["killed"]) == {
            "agentwire-council",
            "council-brain",
            "council-gut",
        }
        assert state.read_sitting() is None

    def test_no_sitting(self, mocks, capsys):
        assert cli.cmd_council_stop(_args(json=True)) == 1


class TestStatus:
    def test_not_running(self, mocks, capsys):
        assert cli.cmd_council_status(_args(json=True)) == 0
        assert _payload(capsys)["running"] is False

    def test_liveness_and_prompts(self, mocks, capsys):
        _start(mocks)
        capsys.readouterr()
        mocks["live"].discard("council-gut")
        cli.cmd_council_ask(_args(prompt="ship it?", file=None, json=True))
        inbox.write_reply(1, "brain", "take", "yes")
        capsys.readouterr()

        cli.cmd_council_status(_args(json=True))
        payload = _payload(capsys)
        alive = {s["soul"]: s["alive"] for s in payload["souls"]}
        assert alive == {"brain": True, "gut": False}
        assert payload["prompts"][0]["pending"] == ["gut"]


class TestAsk:
    def test_inbox_before_send_and_fanout(self, mocks, capsys, monkeypatch):
        _start(mocks)
        capsys.readouterr()

        def send_checking(session, message, marker, retries=1):
            # The inbox must exist before any soul could conceivably reply.
            assert inbox.replies_dir(1).is_dir()
            mocks["sent"].append((session, message))
            return True

        monkeypatch.setattr(cli, "send_verified", send_checking)
        assert cli.cmd_council_ask(_args(prompt="ship it?", file=None, json=True)) == 0
        payload = _payload(capsys)
        assert payload["prompt_id"] == 1
        assert set(payload["sent_to"]) == {"brain", "gut"}
        sessions = [s for s, _ in mocks["sent"]]
        assert set(sessions) == {"council-brain", "council-gut"}
        msg = mocks["sent"][0][1]
        assert "[COUNCIL PROMPT #1]" in msg
        assert "council reply --prompt 1" in msg

    def test_dead_soul_reported(self, mocks, capsys):
        _start(mocks)
        capsys.readouterr()
        mocks["live"].discard("council-gut")
        cli.cmd_council_ask(_args(prompt="x", file=None, json=True))
        payload = _payload(capsys)
        assert payload["sent_to"] == ["brain"]
        assert payload["failed"][0]["soul"] == "gut"

    def test_unconfirmed_delivery_reported(self, mocks, capsys, monkeypatch):
        _start(mocks)
        capsys.readouterr()
        monkeypatch.setattr(cli, "send_verified", lambda s, m, marker, retries=1: False)
        assert cli.cmd_council_ask(_args(prompt="x", file=None, json=True)) == 1
        payload = _payload(capsys)
        assert payload["sent_to"] == []
        assert all(
            f["error"] == "delivery not confirmed in pane" for f in payload["failed"]
        )

    # send_verified mechanics (retry/marker) are covered in
    # tests/unit/test_session_ready.py — the implementation moved there.

    def test_no_sitting(self, mocks, capsys):
        assert cli.cmd_council_ask(_args(prompt="x", file=None, json=True)) == 1

    def test_empty_prompt(self, mocks, capsys, monkeypatch):
        _start(mocks)
        capsys.readouterr()
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        assert cli.cmd_council_ask(_args(prompt=None, file=None, json=True)) == 1


class TestCollect:
    def test_no_wait(self, mocks, capsys):
        _start(mocks)
        cli.cmd_council_ask(_args(prompt="x", file=None, json=True))
        capsys.readouterr()
        args = _args(prompt=None, timeout=120, no_wait=True, json=True)
        assert cli.cmd_council_collect(args) == 0
        payload = _payload(capsys)
        assert payload["prompt_id"] == 1
        assert not payload["complete"]

    def test_complete_round(self, mocks, capsys):
        _start(mocks)
        cli.cmd_council_ask(_args(prompt="x", file=None, json=True))
        inbox.write_reply(1, "brain", "take", "yes")
        inbox.write_reply(1, "gut", "pass", "")
        capsys.readouterr()
        args = _args(prompt=1, timeout=120, no_wait=False, json=True)
        assert cli.cmd_council_collect(args) == 0
        payload = _payload(capsys)
        assert payload["complete"]
        assert {r["kind"] for r in payload["replies"]} == {"take", "pass"}

    def test_no_prompts_yet(self, mocks, capsys):
        _start(mocks)
        capsys.readouterr()
        args = _args(prompt=None, timeout=1, no_wait=True, json=True)
        assert cli.cmd_council_collect(args) == 1


class TestReply:
    def _reply(self, **kw):
        base = dict(
            prompt=None, take=False, ack=False, soul=None, text=None, file=None, json=True
        )
        base.update(kw)
        ns = _args(**base)
        setattr(ns, "pass", kw.get("pass_", False))
        return cli.cmd_council_reply(ns)

    def _setup(self, mocks, capsys):
        _start(mocks)
        cli.cmd_council_ask(_args(prompt="x", file=None, json=True))
        capsys.readouterr()

    def test_take(self, mocks, capsys):
        self._setup(mocks, capsys)
        assert self._reply(take=True, soul="brain", text="my take") == 0
        payload = _payload(capsys)
        assert payload["kind"] == "take"
        assert not payload["followup"]
        assert inbox.list_replies(1)[0].text == "my take"

    def test_soul_inferred_from_session(self, mocks, capsys, monkeypatch):
        self._setup(mocks, capsys)
        monkeypatch.setattr(cli, "current_session", lambda: "council-gut")
        assert self._reply(pass_=True) == 0
        assert _payload(capsys)["soul"] == "gut"

    def test_soul_uninferrable(self, mocks, capsys, monkeypatch):
        self._setup(mocks, capsys)
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        assert self._reply(take=True, text="x") == 1

    def test_exactly_one_kind(self, mocks, capsys):
        self._setup(mocks, capsys)
        assert self._reply(soul="brain") == 1
        capsys.readouterr()
        assert self._reply(take=True, ack=True, soul="brain", text="x") == 1

    def test_take_requires_text(self, mocks, capsys, monkeypatch):
        self._setup(mocks, capsys)
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        assert self._reply(take=True, soul="brain") == 1

    def test_followup_nudges_orchestrator(self, mocks, capsys):
        self._setup(mocks, capsys)
        self._reply(ack=True, soul="brain")
        capsys.readouterr()
        sent_before = len(mocks["sent"])
        assert self._reply(take=True, soul="brain", text="found it") == 0
        payload = _payload(capsys)
        assert payload["followup"]
        assert payload["nudged"] is True
        nudges = mocks["sent"][sent_before:]
        assert len(nudges) == 1
        session, msg = nudges[0]
        assert session == "agentwire-council"
        assert "[COUNCIL FOLLOW-UP]" in msg and "brain" in msg

    def test_initial_reply_does_not_nudge(self, mocks, capsys):
        self._setup(mocks, capsys)
        sent_before = len(mocks["sent"])
        self._reply(take=True, soul="brain", text="x")
        assert len(mocks["sent"]) == sent_before
