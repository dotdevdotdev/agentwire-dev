"""CLI-surface tests for ``agentwire msg`` (#333).

Covers the send-time gone-target warning (#694), ``--body-file`` (#944), and
``msg dead`` (the #693 global default + ``--purge --older-than``).
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
    # Host's real watchdog state must not leak into warning assertions (#1088).
    monkeypatch.setattr("agentwire.limits_cli.drain_warning", lambda: None)
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


    # -- --purge --------------------------------------------------------------

    def test_purge_older_than_keeps_fresh(self, isolate, monkeypatch, capsys):
        self._kill_one(monkeypatch, "s")  # corpse died just now
        rc = msg_cli.cmd_msg_dead(_ns(purge=True, older_than="7d", json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["purged"] == 0  # fresh corpse is younger than 7d
        assert len(inbox.list_dead("s")) == 1


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


