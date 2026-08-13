"""CLI-surface tests for ``agentwire msg`` (#333).

Covers the sharpened empty-recipient reason on ``msg send``, the send-time
gone-target warning (#694), and the ``msg dead`` lister (JSON shape + human
render + the #693 global default).
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
    # Positive-existence checks (#694) default to "unknown" (tmux unreachable)
    # so the host's real session list can't leak into these tests; gone-target
    # scenarios override with a controlled live set.
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
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


class TestSendGoneWarning:
    """#694: queueing to a named session that doesn't currently exist must warn
    the sender in both text and JSON output. It still queues (the target may be
    about to be created) — the warning is the send-time existence signal."""

    def test_warns_in_text(self, isolate, monkeypatch, capsys):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"alive"})
        rc = msg_cli.cmd_msg_send(_ns(to="ghost", kind="done", text=["hi"]))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Queued done" in out
        assert "Warning: session 'ghost' does not currently exist" in out
        assert f"~{inbox.GONE_MAX_ATTEMPTS} min" in out
        assert len(inbox.list_messages("ghost")) == 1  # still queued

    def test_warns_in_json(self, isolate, monkeypatch, capsys):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"alive"})
        rc = msg_cli.cmd_msg_send(_ns(to="ghost", text=["hi"], json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["success"] and out["missing"] == ["ghost"]
        assert "does not currently exist" in out["warnings"][0]

    def test_no_warning_for_live_target(self, isolate, monkeypatch, capsys):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"alive"})
        msg_cli.cmd_msg_send(_ns(to="alive", text=["hi"], json=True))
        out = json.loads(capsys.readouterr().out)
        assert out["missing"] == [] and out["warnings"] == []

    def test_no_warning_when_tmux_unreachable(self, isolate, capsys):
        # live_sessions() is None (isolate default): no positive knowledge of
        # gone-ness, so no warning — mirrors the drain's no-fast-kill rule.
        msg_cli.cmd_msg_send(_ns(to="ghost", text=["hi"]))
        assert "Warning" not in capsys.readouterr().out

    def test_no_warning_for_broadcast(self, isolate, monkeypatch, capsys):
        # @all recipients are live agent sessions by construction — exempt even
        # if the (agent-filtered) targets aren't in the raw live set snapshot.
        monkeypatch.setattr(inbox, "_live_agent_sessions", lambda: ["a"])
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"unrelated"})
        msg_cli.cmd_msg_send(_ns(to="@all", text=["hi"], json=True))
        out = json.loads(capsys.readouterr().out)
        assert out["recipients"] == ["a"] and out["warnings"] == []


class TestDeadGlobalScope:
    """#693: bare ``msg dead`` (no -s) lists EVERY session's graveyard, even
    when invoked from inside a tmux session — the old current-session fallback
    made the global view unreachable exactly where monitoring agents run."""

    def _seed_dead(self, session, tag="x"):
        msg = inbox.Message(
            id=f"1000-{tag}", sender="w", to=session, kind="done",
            text=f"corpse {tag}", ts=1000, attempts=inbox.MAX_ATTEMPTS,
            reason="box_not_empty", dead_ts=1000,
        )
        inbox._write_message(inbox.dead_dir(session) / f"{msg.id}.json", msg)

    def test_bare_dead_is_global_from_inside_a_session(
        self, isolate, monkeypatch, capsys
    ):
        self._seed_dead("other-session")
        # Caller sits INSIDE a tmux session with a clean graveyard of its own —
        # the old code scoped to it and reported "No dead-lettered messages."
        monkeypatch.setattr(msg_cli, "_current_session", lambda: "me")
        rc = msg_cli.cmd_msg_dead(_ns())
        assert rc == 0
        out = capsys.readouterr().out
        assert "other-session" in out and "corpse" in out

    def test_bare_dead_json_is_global(self, isolate, monkeypatch, capsys):
        self._seed_dead("a", "one")
        self._seed_dead("b", "two")
        monkeypatch.setattr(msg_cli, "_current_session", lambda: "a")
        rc = msg_cli.cmd_msg_dead(_ns(json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["total"] == 2
        assert sorted(g["session"] for g in out["sessions"]) == ["a", "b"]

    def test_explicit_session_still_scopes(self, isolate, capsys):
        self._seed_dead("a", "one")
        self._seed_dead("b", "two")
        rc = msg_cli.cmd_msg_dead(_ns(session="a", json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["total"] == 1 and out["sessions"][0]["session"] == "a"


class TestDeadLister:
    def _kill_one(self, monkeypatch, session="s"):
        from agentwire import prompt_router

        inbox.enqueue(session, "stuck", sender="x")
        monkeypatch.setattr("agentwire.usage_limit._capture", lambda s, **kw: "dummy")
        monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "dummy")
        # Box content must CHANGE between sweeps to keep penalizing — identical
        # content across sweeps is the no-penalty box_static path (#669).
        drafts = iter(f"draft content {i}" for i in range(inbox.MAX_ATTEMPTS + 1))
        monkeypatch.setattr(
            prompt_router, "input_box_content_sgr", lambda vis: next(drafts)
        )
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


class TestSendBodyFile:
    """--body-file (#944): a code-bearing body never transits shell argv."""

    HOSTILE = "run `voice` then $(rm -rf /) and `ingest`\nline two\n"

    def _live(self, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orch"})

    def test_body_file_preserved_verbatim(self, isolate, monkeypatch, tmp_path):
        self._live(monkeypatch)
        p = tmp_path / "body.md"
        p.write_text(self.HOSTILE)
        rc = msg_cli.cmd_msg_send(_ns(to="orch", body_file=str(p)))
        assert rc == 0
        msgs = inbox.list_messages("orch")
        assert len(msgs) == 1
        # Backticks and $() intact — the whole point of the flag.
        assert msgs[0].text == self.HOSTILE

    def test_dash_reads_stdin(self, isolate, monkeypatch):
        import io

        self._live(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO(self.HOSTILE))
        rc = msg_cli.cmd_msg_send(_ns(to="orch", body_file="-"))
        assert rc == 0
        assert inbox.list_messages("orch")[0].text == self.HOSTILE

    def test_mutually_exclusive_with_text(self, isolate, tmp_path, capsys):
        p = tmp_path / "body.md"
        p.write_text("x")
        rc = msg_cli.cmd_msg_send(_ns(to="orch", body_file=str(p), text=["hi"]))
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().out
        assert inbox.list_messages("orch") == []

    def test_unreadable_path_fails_loudly(self, isolate, tmp_path, capsys):
        rc = msg_cli.cmd_msg_send(
            _ns(to="orch", body_file=str(tmp_path / "nope.md"), json=True))
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is False and "--body-file" in out["error"]
        assert inbox.list_messages("orch") == []

    def test_empty_file_is_empty_message(self, isolate, tmp_path, capsys):
        p = tmp_path / "empty.md"
        p.write_text("   \n")
        rc = msg_cli.cmd_msg_send(_ns(to="orch", body_file=str(p)))
        assert rc == 1
        assert "Usage" in capsys.readouterr().out

    def test_positional_text_path_unchanged(self, isolate, monkeypatch):
        self._live(monkeypatch)
        rc = msg_cli.cmd_msg_send(_ns(to="orch", text=["plain", "words"]))
        assert rc == 0
        assert inbox.list_messages("orch")[0].text == "plain words"
