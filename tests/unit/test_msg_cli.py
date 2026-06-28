"""CLI-surface tests for ``agentwire msg`` (#333).

Covers the sharpened empty-recipient reason on ``msg send`` and the new
``msg dead`` lister (JSON shape + human render).
"""

import json
from types import SimpleNamespace

import pytest

from agentwire import inbox, msg_cli


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(msg_cli, "_current_session", lambda: None)
    return tmp_path


def _ns(**kw):
    base = dict(
        json=False, session=None, to=None, kind="note", from_session="orch",
        text=None, purge=False, older_than=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestSendEmptyRecipient:
    def test_at_all_no_live_sessions_json(self, isolate, monkeypatch, capsys):
        monkeypatch.setattr(inbox, "_live_agent_sessions", lambda: [])
        rc = msg_cli.cmd_msg_send(_ns(to="@all", text=["hi"], json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["recipients"] == [] and out["reason"] == "@all → no live agent sessions"

    def test_at_all_no_live_sessions_human(self, isolate, monkeypatch, capsys):
        monkeypatch.setattr(inbox, "_live_agent_sessions", lambda: [])
        msg_cli.cmd_msg_send(_ns(to="@all", text=["hi"]))
        assert "no live agent sessions" in capsys.readouterr().out


class TestDeadLister:
    def _kill_one(self, monkeypatch, session="s"):
        from agentwire import prompt_router

        inbox.enqueue(session, "stuck", sender="x")
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s: "dummy")
        monkeypatch.setattr(prompt_router, "input_box_content", lambda vis: "draft content")
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session(session)

    def test_dead_json(self, isolate, monkeypatch, capsys):
        self._kill_one(monkeypatch)
        rc = msg_cli.cmd_msg_dead(_ns(session="s", json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["total"] == 1
        dead = out["sessions"][0]["dead"][0]
        assert dead["reason"] == "box_not_empty" and dead["dead_ts"] > 0

    def test_dead_human(self, isolate, monkeypatch, capsys):
        self._kill_one(monkeypatch)
        msg_cli.cmd_msg_dead(_ns(session="s"))
        out = capsys.readouterr().out
        assert "dead-lettered" in out and "box_not_empty" in out

    def test_dead_none(self, isolate, capsys):
        rc = msg_cli.cmd_msg_dead(_ns(session="s"))
        assert rc == 0
        assert "No dead-lettered messages" in capsys.readouterr().out

    # -- --purge --------------------------------------------------------------

    def test_purge_all_human(self, isolate, monkeypatch, capsys):
        self._kill_one(monkeypatch, "s")
        rc = msg_cli.cmd_msg_dead(_ns(purge=True))
        assert rc == 0
        assert "Purged 1" in capsys.readouterr().out
        assert inbox.list_dead("s") == []

    def test_purge_scoped_json(self, isolate, monkeypatch, capsys):
        self._kill_one(monkeypatch, "s")
        self._kill_one(monkeypatch, "t")
        rc = msg_cli.cmd_msg_dead(_ns(session="s", purge=True, json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["purged"] == 1 and out["session"] == "s"
        assert inbox.list_dead("s") == [] and len(inbox.list_dead("t")) == 1

    def test_purge_global_ignores_current_session(self, isolate, monkeypatch):
        # purge with no -s clears the WHOLE graveyard, never just the current session
        self._kill_one(monkeypatch, "s")
        self._kill_one(monkeypatch, "t")
        monkeypatch.setattr(msg_cli, "_current_session", lambda: "s")
        rc = msg_cli.cmd_msg_dead(_ns(purge=True))
        assert rc == 0
        assert inbox.list_dead("s") == [] and inbox.list_dead("t") == []

    def test_purge_older_than_keeps_fresh(self, isolate, monkeypatch, capsys):
        self._kill_one(monkeypatch, "s")  # corpse died just now
        rc = msg_cli.cmd_msg_dead(_ns(purge=True, older_than="7d", json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["purged"] == 0  # fresh corpse is younger than 7d
        assert len(inbox.list_dead("s")) == 1

    def test_purge_invalid_older_than(self, isolate, capsys):
        rc = msg_cli.cmd_msg_dead(_ns(purge=True, older_than="lol"))
        assert rc == 2
        assert "Invalid --older-than" in capsys.readouterr().err
