"""Tests for council minutes (#708) — synthetic prompt dirs, never live sittings.

Covers the pure renderer (``agentwire/council/minutes.py``), the
``council minutes`` CLI handler, and the ``council stop`` auto-minutes
integration. Artifacts land in a tmp dir and the portal notification is mocked,
so nothing touches ``~/.agentwire`` or the network.
"""

import argparse
import json

import pytest

from agentwire.council import cli, inbox, minutes, state

NAME = "proj"


@pytest.fixture(autouse=True)
def council_root(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "COUNCIL_ROOT", tmp_path / "council")
    # Keep resolution hermetic — no real `git` call for the cwd-slug seed.
    monkeypatch.setattr(state, "default_name", lambda cwd=None: "nomatch")
    return tmp_path / "council"


@pytest.fixture(autouse=True)
def artifacts_root(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    monkeypatch.setattr(minutes, "artifacts_dir", lambda: root)
    return root


@pytest.fixture(autouse=True)
def portal_notices(monkeypatch):
    """Mock the best-effort portal artifact notification; record calls."""
    calls = []

    def fake_notify(url, title):
        calls.append((url, title))
        return True

    monkeypatch.setattr(cli, "notify_artifact", fake_notify)
    return calls


def seat(name=NAME, roster=("brain", "gut")):
    state.write_sitting(
        name,
        state.Sitting(
            orchestrator=state.orchestrator_for(name),
            roster=list(roster),
            sessions={lens: state.session_for(name, lens) for lens in roster},
            started_at="2026-07-04T10:00:00+00:00",
        ),
    )


def make_round(pid=1, question="Should we ship X?", roster=("brain", "gut"), name=NAME):
    inbox.create_prompt(name, pid, question, list(roster))


def _args(**kw):
    ns = argparse.Namespace()
    kw.setdefault("name", NAME)
    kw.setdefault("json", True)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)




class TestRender:
    def _record(self, **kw):
        seat()
        make_round(**kw)
        inbox.write_reply(NAME, kw.get("pid", 1), "brain", "take", "Ship it.")
        return minutes.gather(NAME)


    def test_verbatim_text_is_escaped(self):
        html = minutes.render_html(
            self._record(question="<script>alert(1)</script> & so on")
        )
        assert "<script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html








class TestMinutesCmd:
    def _run(self, **kw):
        kw.setdefault("prompt", None)
        kw.setdefault("synthesis", None)
        return cli.cmd_council_minutes(_args(**kw))

    def test_renders_prints_path_and_notifies(self, capsys, portal_notices):
        seat()
        make_round()
        assert self._run() == 0
        payload = _payload(capsys)
        assert payload["success"] and payload["notified"]
        assert payload["path"].endswith(f"council-{NAME}-minutes/index.html")
        assert portal_notices == [
            (f"council-{NAME}-minutes/index.html", f"Council minutes — {NAME}")
        ]









