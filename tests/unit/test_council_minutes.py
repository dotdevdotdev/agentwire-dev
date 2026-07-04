"""Tests for council minutes (#708) — synthetic prompt dirs, never live sittings.

Covers the pure renderer (``agentwire/council/minutes.py``), the
``council minutes`` CLI handler, and the ``council stop`` auto-minutes
integration. Artifacts land in a tmp dir and the portal open is mocked, so
nothing touches ``~/.agentwire`` or the network.
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
def portal_opens(monkeypatch):
    """Mock the best-effort portal window open; record calls."""
    calls = []

    def fake_open(url, title):
        calls.append((url, title))
        return True

    monkeypatch.setattr(cli, "open_artifact_window", fake_open)
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


class TestGather:
    def test_no_prompts_is_none(self):
        seat()
        assert minutes.gather(NAME) is None

    def test_collects_question_and_attributed_replies(self):
        seat()
        make_round()
        inbox.write_reply(NAME, 1, "brain", "take", "Ship it.")
        inbox.write_reply(NAME, 1, "gut", "pass", "")
        record = minutes.gather(NAME)
        assert record["name"] == NAME
        assert not record["archived"]
        assert record["roster"] == ["brain", "gut"]
        [prompt] = record["prompts"]
        assert prompt["id"] == 1
        assert prompt["question"] == "Should we ship X?"
        by_soul = {r["soul"]: r for r in prompt["replies"]}
        assert by_soul["brain"]["kind"] == "take"
        assert by_soul["brain"]["text"] == "Ship it."
        assert by_soul["gut"]["kind"] == "pass"

    def test_followups_kept_verbatim(self):
        seat()
        make_round()
        inbox.write_reply(NAME, 1, "brain", "ack", "")
        inbox.write_reply(NAME, 1, "brain", "take", "Researched: yes.")
        kinds = [r["kind"] for r in minutes.gather(NAME)["prompts"][0]["replies"]]
        assert kinds == ["ack", "followup"]

    def test_replies_carry_no_local_paths(self):
        seat()
        make_round()
        inbox.write_reply(NAME, 1, "brain", "take", "x")
        [reply] = minutes.gather(NAME)["prompts"][0]["replies"]
        assert set(reply) == {"soul", "kind", "text", "written_at"}

    def test_dismissed_sitting_renders_from_archive(self):
        seat()
        make_round()
        inbox.write_reply(NAME, 1, "brain", "take", "x")
        state.clear_sitting(NAME)
        record = minutes.gather(NAME)
        assert record["archived"]
        assert record["dismissed_at"]
        assert record["roster"] == ["brain", "gut"]
        assert record["prompts"][0]["question"] == "Should we ship X?"

    def test_prompt_filter(self):
        seat()
        make_round(pid=1, question="first")
        make_round(pid=2, question="second")
        record = minutes.gather(NAME, [2])
        assert [p["id"] for p in record["prompts"]] == [2]
        assert minutes.gather(NAME, [99]) is None


class TestRender:
    def _record(self, **kw):
        seat()
        make_round(**kw)
        inbox.write_reply(NAME, kw.get("pid", 1), "brain", "take", "Ship it.")
        return minutes.gather(NAME)

    def test_contains_question_souls_and_synthesis(self):
        html = minutes.render_html(self._record(), synthesis="We ship.")
        assert "Should we ship X?" in html
        assert "brain" in html
        assert "We ship." in html
        assert f"Council minutes — {NAME}" in html

    def test_verbatim_text_is_escaped(self):
        html = minutes.render_html(
            self._record(question="<script>alert(1)</script> & so on")
        )
        assert "<script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_self_contained_no_external_fetches(self):
        html = minutes.render_html(self._record(), synthesis="s")
        assert "<style>" in html  # inline CSS only
        for marker in ("<script", "<link", "src=", "@import", "url("):
            assert marker not in html
        assert "prefers-color-scheme" in html  # theme-aware

    def test_synthesis_omitted_when_absent(self):
        html = minutes.render_html(self._record())
        assert "Synthesis" not in html

    def test_synthesis_renders_once_single_and_multi_prompt(self):
        seat()
        make_round(pid=1)
        html = minutes.render_html(minutes.gather(NAME), synthesis="verdict")
        assert html.count(">Synthesis<") == 1
        make_round(pid=2, question="round two")
        html = minutes.render_html(minutes.gather(NAME), synthesis="verdict")
        assert html.count(">Synthesis<") == 1

    def test_textless_pass_and_ack_get_placeholders(self):
        seat()
        make_round()
        inbox.write_reply(NAME, 1, "brain", "pass", "")
        inbox.write_reply(NAME, 1, "gut", "ack", "")
        html = minutes.render_html(minutes.gather(NAME))
        assert "Nothing to add through this lens." in html
        assert "Researching — follow-up coming." in html


class TestWriteMinutes:
    def test_writes_index_html(self, artifacts_root):
        seat()
        make_round()
        path = minutes.write_minutes(NAME, synthesis="s")
        assert path == artifacts_root / f"council-{NAME}-minutes" / "index.html"
        assert "Should we ship X?" in path.read_text()

    def test_zero_prompts_writes_nothing(self, artifacts_root):
        seat()
        assert minutes.write_minutes(NAME) is None
        assert not (artifacts_root / f"council-{NAME}-minutes").exists()


class TestMinutesCmd:
    def _run(self, **kw):
        kw.setdefault("prompt", None)
        kw.setdefault("synthesis", None)
        return cli.cmd_council_minutes(_args(**kw))

    def test_renders_prints_path_and_opens_window(self, capsys, portal_opens):
        seat()
        make_round()
        assert self._run() == 0
        payload = _payload(capsys)
        assert payload["success"] and payload["opened"]
        assert payload["path"].endswith(f"council-{NAME}-minutes/index.html")
        assert portal_opens == [
            (f"council-{NAME}-minutes/index.html", f"Council minutes — {NAME}")
        ]

    def test_portal_down_is_best_effort(self, capsys, monkeypatch):
        seat()
        make_round()
        monkeypatch.setattr(cli, "open_artifact_window", lambda url, title: False)
        assert self._run() == 0
        payload = _payload(capsys)
        assert payload["success"] and payload["opened"] is False

    def test_zero_prompts_errors(self, capsys):
        seat()
        assert self._run() == 1
        assert "no prompt history" in _payload(capsys)["error"]

    def test_prompt_selects_single_round(self, capsys):
        seat()
        make_round(pid=1, question="first question")
        make_round(pid=2, question="second question")
        assert self._run(prompt="1") == 0
        html = open(_payload(capsys)["path"]).read()
        assert "first question" in html
        assert "second question" not in html

    def test_unknown_prompt_id_lists_available(self, capsys):
        seat()
        make_round(pid=1)
        assert self._run(prompt="7") == 1
        err = _payload(capsys)["error"]
        assert "#7" in err and "available: 1" in err

    def test_non_numeric_prompt_rejected(self, capsys):
        seat()
        make_round()
        assert self._run(prompt="latest") == 1
        assert "'latest'" in _payload(capsys)["error"]

    def test_synthesis_from_file(self, capsys, tmp_path):
        seat()
        make_round()
        synth = tmp_path / "synthesis.md"
        synth.write_text("The council leans yes.")
        assert self._run(synthesis=str(synth)) == 0
        assert "The council leans yes." in open(_payload(capsys)["path"]).read()

    def test_works_for_dismissed_sitting(self, capsys):
        seat()
        make_round()
        state.clear_sitting(NAME)
        assert self._run() == 0
        assert _payload(capsys)["success"]


class TestStopMinutes:
    @pytest.fixture(autouse=True)
    def quiet_sessions(self, monkeypatch):
        monkeypatch.setattr(cli, "list_live_sessions", lambda: set())
        monkeypatch.setattr(cli, "kill_session", lambda name: True)

    def _stop(self, **kw):
        kw.setdefault("minutes", None)
        kw.setdefault("synthesis", None)
        return cli.cmd_council_stop(_args(**kw))

    def test_default_renders_when_prompts_exist(self, capsys, artifacts_root):
        seat()
        make_round()
        inbox.write_reply(NAME, 1, "brain", "take", "x")
        assert self._stop() == 0
        payload = _payload(capsys)
        assert payload["minutes"].endswith("index.html")
        assert (artifacts_root / f"council-{NAME}-minutes" / "index.html").exists()
        assert state.read_sitting(NAME) is None  # still cleared

    def test_default_skips_when_no_prompts(self, capsys, artifacts_root):
        seat()
        assert self._stop() == 0
        assert _payload(capsys)["minutes"] is None
        assert not (artifacts_root / f"council-{NAME}-minutes").exists()

    def test_no_minutes_flag_skips(self, capsys, artifacts_root):
        seat()
        make_round()
        assert self._stop(minutes=False) == 0
        assert _payload(capsys)["minutes"] is None
        assert not (artifacts_root / f"council-{NAME}-minutes").exists()

    def test_synthesis_lands_in_minutes(self, capsys, artifacts_root):
        seat()
        make_round()
        assert self._stop(synthesis="Final word.") == 0
        html = (artifacts_root / f"council-{NAME}-minutes" / "index.html").read_text()
        assert "Final word." in html
