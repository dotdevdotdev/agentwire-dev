"""#1271: ``conversation_ids`` follows Claude's in-session id changes.

A single-launch test cannot see this bug: the chain is written at launch, so a
session that never ``/clear``s always restarts correctly. Every test here takes
a SECOND step after the launch.
"""

import io
import json
import types

import pytest

from agentwire import doctor_cli, hooks_cli, restart_cli
from agentwire.history import encode_project_path

TURN = '{"type":"user","message":{"role":"user","content":"hi"}}\n'


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
    monkeypatch.setattr("agentwire.history.claude_projects_dir", lambda: tmp_path / "projects")
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    (tmp_path / "projects").mkdir()
    return types.SimpleNamespace(root=tmp_path, cwd=cwd, projects=tmp_path / "projects")


@pytest.fixture
def pane(monkeypatch):
    """Where the hook believes it runs: (session, pane_index)."""
    where = types.SimpleNamespace(session="sess", index=0)
    monkeypatch.setattr("agentwire.pane_manager.get_current_session", lambda: where.session)
    monkeypatch.setattr("agentwire.pane_manager.get_current_pane_index", lambda: where.index)
    return where


def transcript(store, cid):
    d = store.projects / encode_project_path(str(store.cwd))
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.jsonl").write_text(TURN)
    return d / f"{cid}.jsonl"


def launch(store, session="sess"):
    from agentwire.core import build_agent_command, record_session_launch

    agent = build_agent_command("bypass")
    record_session_launch(session, agent, store.cwd)
    return agent.conversation_id


def session_start(cid, source="clear"):
    return hooks_cli.record_session_start({"session_id": cid, "source": source})


def chain(session="sess"):
    from agentwire.core import load_session_metadata

    return load_session_metadata(session)["conversation_ids"]


def test_restart_resumes_the_post_clear_conversation(store, pane, monkeypatch):
    launched = launch(store)
    transcript(store, launched)
    session_start("after-clear-1")
    transcript(store, "after-clear-1")
    session_start("after-clear-2")
    transcript(store, "after-clear-2")

    assert chain() == [launched, "after-clear-1", "after-clear-2"]

    captured = []
    monkeypatch.setattr(restart_cli, "tmux_session_exists", lambda n: False)
    monkeypatch.setattr(restart_cli, "_notify_portal_sessions_changed", lambda: None)
    monkeypatch.setattr(restart_cli.pane_manager, "get_current_session", lambda: None)
    monkeypatch.setattr(
        restart_cli, "_launch_tmux_session",
        lambda name, path, env, cmd: captured.append(cmd),
    )
    args = types.SimpleNamespace(session="sess", json=False, no_wait=True)
    assert restart_cli.cmd_restart(args) == 0
    assert '--resume "after-clear-2" --fork-session' in captured[0]


def test_a_cleared_but_unprompted_conversation_falls_back_to_the_last_real_one(store, pane):
    launched = launch(store)
    transcript(store, launched)
    session_start("cleared-never-prompted")  # no transcript: written lazily

    resume_id, _ = restart_cli.resolve_resume_target(chain(), str(store.cwd))
    assert resume_id == launched


def test_the_hook_is_idempotent_and_resume_moves_an_old_id_to_the_tail(store, pane):
    from agentwire.core import record_conversation_change

    launched = launch(store)
    assert record_conversation_change("sess", launched) is False  # the launch's own id
    session_start("b")
    assert session_start("b").startswith("unchanged")
    assert chain() == [launched, "b"]

    # /resume back to the launch conversation: it is now the CURRENT one.
    session_start(launched, source="resume")
    assert chain() == ["b", launched]


def test_the_hook_never_writes_outside_its_own_session_record(store, pane):
    launch(store)
    before = chain()

    pane.index = 1  # a worker pane is not a session
    assert session_start("worker-conv").startswith("skipped: worker pane")

    pane.index, pane.session = 0, "no-such-session"  # no launch record: nothing to extend
    assert session_start("x").startswith("unchanged")
    from agentwire.core import load_session_metadata, session_metadata_path

    assert not session_metadata_path("no-such-session").exists()
    assert load_session_metadata("no-such-session") == {}
    assert chain() == before


def test_the_hook_command_exits_zero_on_every_bad_input(store, pane, monkeypatch, capsys):
    launch(store)
    bad = ["", "{{{", "[1]", '{"no":"id"}', '{"session_id": 5}']
    codes = []
    for raw in bad:
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        codes.append(hooks_cli.cmd_hooks_session_start(types.SimpleNamespace()))

    def boom(*_):
        raise OSError("read-only store")

    monkeypatch.setattr("agentwire.core.store_session_metadata", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "new"})))
    codes.append(hooks_cli.cmd_hooks_session_start(types.SimpleNamespace()))

    assert codes == [0] * len(codes)
    assert "read-only store" in capsys.readouterr().err  # logged, not swallowed silently


class TestDoctorUnrecordedTranscripts:
    @pytest.fixture(autouse=True)
    def live(self, monkeypatch):
        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda n: True)

    def test_newer_unrecorded_transcript_is_stated(self, store):
        import os

        launched = launch(store)
        head = transcript(store, launched)
        cleared = transcript(store, "cleared")
        os.utime(head, (1000, 1000))
        os.utime(cleared, (2000, 2000))

        [f] = doctor_cli.scan_unrecorded_transcripts(["sess"])
        assert (f["head"], f["newer"]) == (launched, ["cleared"])

    def test_a_recorded_or_older_transcript_is_silent(self, store, pane):
        import os

        launched = launch(store)
        head = transcript(store, launched)
        older = transcript(store, "older")
        os.utime(older, (500, 500))
        os.utime(head, (1000, 1000))
        assert doctor_cli.scan_unrecorded_transcripts(["sess"]) == []

        newer = transcript(store, "cleared")
        os.utime(newer, (2000, 2000))
        session_start("cleared")  # the hook caught it
        os.utime(newer, (2000, 2000))
        assert doctor_cli.scan_unrecorded_transcripts(["sess"]) == []
