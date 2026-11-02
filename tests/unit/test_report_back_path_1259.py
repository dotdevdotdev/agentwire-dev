"""#1259 — a delegated worker with no parent must learn that, loudly, at every layer."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentwire import core, inbox, msg_cli, pane_manager, prompt_router
from agentwire.__main__ import build_parser
from agentwire.notify_cli import cmd_notify_parent
from agentwire.roles import load_roles


def _spawn_warning(argv, *, caller_cwd, target):
    """Real arg parsing → the same created_by decision cmd_new makes → warning."""
    args = build_parser().parse_args(argv)
    caller = "orch"
    created_by = args.created_by
    if created_by is None:
        created_by = core.resolve_default_created_by(caller, Path(target))
    return core.unparented_delegation_warning(
        "proj-child", caller, created_by, args.prompt)


@pytest.fixture
def caller_in_repo_a(monkeypatch):
    monkeypatch.setattr(core, "_live_session_cwd", lambda s: Path("/a"))
    monkeypatch.setattr(core, "_same_project", lambda x, y: str(y).startswith("/a"))
    from agentwire import services
    monkeypatch.setattr(services, "is_service_session", lambda s: False)


class TestSpawnWarning:
    def test_warning_matrix(self, caller_in_repo_a):
        base = ["worktree", "child", "-p", "x"]
        cases = {
            "cross-project + prompt": (base + ["--prompt", "do it"], "/b", True),
            "same-project + prompt": (base + ["--prompt", "do it"], "/a/x", False),
            "cross-project, no prompt": (base, "/b", False),
            "cross-project + prompt + --created-by": (
                base + ["--prompt", "do it", "--created-by", "orch"], "/b", False),
            "cross-project + prompt + explicit root": (
                base + ["--prompt", "do it", "--created-by", ""], "/b", False),
        }
        bad = {}
        for label, (argv, target, expect) in cases.items():
            w = _spawn_warning(argv, caller_cwd="/a", target=target)
            if bool(w) != expect:
                bad[label] = w
            if expect:
                assert "--created-by orch" in w and "proj-child" in w
        assert not bad, bad


class TestNotifyParentNoTarget:
    def test_fails_loudly_and_never_suggests_to_parent(self, monkeypatch, capsys):
        monkeypatch.setattr(pane_manager, "get_current_session", lambda: "child")
        monkeypatch.setattr(pane_manager, "get_current_pane_index", lambda: 0)
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda s, p: None)
        rc = cmd_notify_parent(SimpleNamespace(
            text=["done"], to=None, json=False, queued=False, on_idle=False,
            raw=False, body_file=None))
        out = capsys.readouterr()
        text = out.out + out.err
        assert rc == 1
        assert "LAST line of your final message" in text
        assert "--to parent" not in text


class TestMsgToRoleWord:
    @pytest.fixture(autouse=True)
    def _iso(self, tmp_path, monkeypatch):
        monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
        monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "ev.jsonl")
        monkeypatch.setattr(msg_cli, "_current_session", lambda: None)
        monkeypatch.setattr("agentwire.limits_cli.drain_warning", lambda: None)
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orch"})

    def test_role_words_rejected_at_enqueue(self, capsys):
        bad = []
        for word in sorted(inbox.ROLE_WORDS):
            rc = msg_cli.cmd_msg_send(SimpleNamespace(
                json=False, to=word, kind="done", from_session="w",
                text=["hi"], body_file=None, ref=""))
            if rc != 1 or inbox.list_messages(word):
                bad.append(word)
        assert not bad, bad
        assert "LAST line of your final message" in capsys.readouterr().out

    def test_a_live_session_with_that_name_is_still_addressable(self, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})
        assert inbox.resolve_targets("orchestrator", "w") == ["orchestrator"]


def test_worker_and_reviewer_prompts_carry_the_no_parent_rule():
    names = ["worker", "worker-worktree", "reviewer", "reviewer-worktree"]
    roles, missing = load_roles(names)
    assert not missing, missing
    bad = [
        r.name for r in roles
        if "LAST line of your final message" not in r.instructions
        or "do not guess a session name" not in r.instructions
    ]
    assert not bad, bad
