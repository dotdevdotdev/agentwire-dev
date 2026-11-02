"""#1261 — a wrong parent/target name fails when it is NAMED, not hours later as a dead-letter."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentwire import core, inbox, msg_cli, pane_manager, services, session_cli
from agentwire.__main__ import build_parser
from agentwire.notify_cli import cmd_notify_parent

def _all(capsys):
    o = capsys.readouterr()
    return o.out + o.err


LIVE = {"agentwire", "orch", "agentwire-portal"}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "ev.jsonl")
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(inbox, "live_sessions", lambda: set(LIVE))
    monkeypatch.setattr(msg_cli, "_current_session", lambda: "w")
    monkeypatch.setattr("agentwire.limits_cli.drain_warning", lambda: None)


class TestAutoParent:
    @pytest.fixture
    def caller_in_repo_a(self, monkeypatch):
        monkeypatch.setattr(core, "_live_session_cwd", lambda s: Path("/a"))
        monkeypatch.setattr(core, "_same_project", lambda x, y: str(y).startswith("/a"))
        monkeypatch.setattr(services, "is_service_session", lambda s: s.startswith("agentwire-p"))

    def test_matrix(self, caller_in_repo_a, monkeypatch):
        def parent(caller, target, delegated):
            return core.resolve_default_created_by(caller, Path(target), delegated=delegated)

        got = {
            "delegated cross-project": parent("orch", "/b", True),
            "no prompt cross-project": parent("orch", "/b", False),
            "same-project no prompt": parent("orch", "/a/x", False),
            "same-project delegated": parent("orch", "/a/x", True),
            "no caller": parent(None, "/b", True),
            "service caller": parent("agentwire-portal", "/b", True),
        }
        want = {
            "delegated cross-project": "orch",
            "no prompt cross-project": None,
            "same-project no prompt": "orch",
            "same-project delegated": "orch",
            "no caller": None,
            "service caller": None,
        }
        assert got == want
        # caller that is not a live session cannot be auto-parented
        monkeypatch.setattr(core, "_live_session_cwd", lambda s: None)
        assert parent("ghost", "/b", True) is None

    def test_warning_fires_only_when_auto_parent_failed(self, caller_in_repo_a, monkeypatch):
        def warn(caller, target, prompt, explicit=None):
            created_by = explicit
            if created_by is None:
                created_by = core.resolve_default_created_by(
                    caller, Path(target), delegated=bool(prompt))
            return core.unparented_delegation_warning("p-child", caller, created_by, prompt)

        assert warn("orch", "/b", "do it") is None
        monkeypatch.setattr(core, "_live_session_cwd", lambda s: None)
        assert "NO parent" in warn("ghost", "/b", "do it")
        assert warn("ghost", "/b", None) is None


class TestSpawnValidation:
    def test_typo_gets_did_you_mean(self):
        err = session_cli.created_by_error("agentwire-dev")
        assert "did you mean: agentwire" in err and "NOT your repo name" in err

    def test_empty_and_none_and_live_pass(self):
        assert [session_cli.created_by_error(x) for x in (None, "", "orch")] == [None] * 3

    def test_tmux_unreachable_is_not_a_verdict(self, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: None)
        assert session_cli.created_by_error("bogus") is None

    def test_record_alone_is_not_enough_for_a_parent(self, tmp_path):
        (tmp_path / "cfg" / "sessions" / "old").mkdir(parents=True)
        (tmp_path / "cfg" / "sessions" / "old" / "metadata.json").write_text("{}")
        assert session_cli.created_by_error("old")

    def test_new_fails_before_creating_a_worktree(self, monkeypatch, capsys):
        monkeypatch.setattr(
            session_cli, "_spawn_worktree",
            lambda *a, **k: pytest.fail("worktree created before --created-by was validated"),
            raising=False)
        args = build_parser().parse_args(["new", "-s", "proj/br", "--created-by", "agentwire-dev"])
        assert session_cli.cmd_new(args) == 1
        assert "did you mean: agentwire" in _all(capsys)

    def test_worktree_fails_before_anything_exists(self, tmp_path, capsys):
        args = build_parser().parse_args(
            ["worktree", "x", "-p", str(tmp_path), "--created-by", "agentwire-dev"])
        assert session_cli.cmd_worktree(args) == 1
        assert "did you mean: agentwire" in _all(capsys)
        assert not (tmp_path / "x").exists()

    def test_remote_spawn_is_exempt(self, capsys):
        args = build_parser().parse_args(["new", "-s", "web@nowhere", "--created-by", "bogus"])
        session_cli.cmd_new(args)
        assert "--created-by" not in _all(capsys)


def _send(to):
    return msg_cli.cmd_msg_send(SimpleNamespace(
        json=False, to=to, kind="done", from_session=None, text=["hi"], body_file=None, ref=""))


class TestMessageTargets:
    def test_typo_target_fails_and_enqueues_nothing(self, capsys):
        assert _send("agentwire-dev") == 1
        assert "did you mean: agentwire" in capsys.readouterr().out
        assert inbox.list_messages("agentwire-dev") == []

    def test_live_target_queues(self):
        assert _send("orch") == 0 and len(inbox.list_messages("orch")) == 1

    def test_known_but_not_live_target_still_defers(self, tmp_path):
        rec = tmp_path / "cfg" / "sessions" / "restarting"
        rec.mkdir(parents=True)
        (rec / "metadata.json").write_text("{}")
        assert _send("restarting") == 0 and len(inbox.list_messages("restarting")) == 1

    def test_broadcast_and_remote_pass(self):
        assert inbox.unknown_target_error("@all") is None
        assert inbox.unknown_target_error("web@box") is None

    def test_notify_parent_queued_typo_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(pane_manager, "get_current_session", lambda: "child")
        monkeypatch.setattr(pane_manager, "get_current_pane_index", lambda: 0)
        rc = cmd_notify_parent(SimpleNamespace(
            text=["done"], to="agentwire-dev", json=False, queued=True, on_idle=False,
            raw=False, body_file=None, quiet=False))
        assert rc == 1
        assert "did you mean: agentwire" in _all(capsys)
        assert inbox.list_messages("agentwire-dev") == []
