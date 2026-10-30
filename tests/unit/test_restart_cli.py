"""Tests for ``agentwire restart`` — relaunch in place, same conversation (#871).

The behaviours worth pinning down are the ones a plausible implementation gets
wrong: re-evaluating the stored launch line (whose ``--session-id`` is
single-use), assuming a recorded id is resumable, and killing the session the
command itself runs in.
"""

import types

import pytest

from agentwire import restart_cli

#: The minimum that makes a transcript a CONVERSATION rather than a metadata
#: stub. A stub is a DEAD id — measured: `claude --resume` answers "No
#: conversation found" while `--session-id` still says "already in use", so it
#: can be neither resumed nor reclaimed (see history.holds_a_conversation).
TURN = '{"type":"user","message":{"role":"user","content":"hi"}}\n'
STUB = '{"type":"ai-title"}\n{"type":"mode","mode":"normal"}\n'


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated ~/.agentwire and ~/.claude/projects, plus a launch cwd."""
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
    monkeypatch.setattr("agentwire.history.claude_projects_dir", lambda: tmp_path / "projects")
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    (tmp_path / "projects").mkdir()
    return types.SimpleNamespace(root=tmp_path, cwd=cwd, projects=tmp_path / "projects")


def write_history(store, cwd, conversation_id):
    """Create the ``.jsonl`` Claude would write on the first turn."""
    from agentwire.history import encode_project_path

    d = store.projects / encode_project_path(str(cwd))
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{conversation_id}.jsonl"
    f.write_text(TURN)
    return f


def record(session="sess", *, cwd, ids=(), posture="bypass", roles=(), **extra):
    from agentwire.core import store_session_metadata

    metadata = {
        "cwd_at_launch": str(cwd),
        "posture": posture,
        "roles": list(roles),
        "conversation_ids": list(ids),
        **extra,
    }
    store_session_metadata(session, metadata)
    return metadata


@pytest.fixture
def launched(monkeypatch):
    """Capture the launch instead of touching tmux."""
    calls = types.SimpleNamespace(launch=[], killed=[], live=True, ready=True)

    monkeypatch.setattr(restart_cli, "tmux_session_exists", lambda n: calls.live)
    monkeypatch.setattr(restart_cli, "_graceful_kill", lambda n: calls.killed.append(n))
    monkeypatch.setattr(
        restart_cli, "_launch_tmux_session",
        lambda name, path, env, cmd: calls.launch.append((name, str(path), env, cmd)),
    )
    monkeypatch.setattr(restart_cli, "_notify_portal_sessions_changed", lambda: None)
    monkeypatch.setattr(restart_cli.pane_manager, "get_current_session", lambda: None)
    monkeypatch.setattr(
        "agentwire.session_ready.wait_for_session_ready",
        lambda name, timeout=0: calls.ready,
    )
    return calls


def run(session="sess", **kw):
    args = types.SimpleNamespace(session=session, json=False, no_wait=False, **kw)
    return restart_cli.cmd_restart(args)


class TestRestartLaunch:
    def test_resume_picks_the_newest_conversation_in_the_chain(self, store):
        write_history(store, store.cwd, "old")
        write_history(store, store.cwd, "new")
        rid, loc = restart_cli.resolve_resume_target(["old", "new"], str(store.cwd))
        assert (rid, loc.status) == ("new", "resumable")

    def test_resumes_the_recorded_conversation(self, store, launched):
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"])

        assert run() == 0
        (name, path, _env, cmd) = launched.launch[0]
        assert name == "sess" and path == str(store.cwd)
        # The flags are resolved by the shell at launch (#901), so they ride
        # in the prelude rather than immediately after `claude`.
        assert '--resume "cid-1" --fork-session --session-id ' in cmd
        assert 'claude "${aw_flags[@]}"' in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert launched.killed == ["sess"]

    def test_mints_a_fresh_session_id_never_reuses_the_recorded_one(self, store, launched):
        """The recorded id goes to ``--resume`` only.

        Re-passing it as ``--session-id`` is fatal ("already in use") and drops
        the pane to a bare shell — the exact zombie the guarded launch exists
        to prevent. Restart regenerates the line rather than replaying the
        stored one for the same reason (#901 made the stored line itself
        re-runnable, which is a different guarantee).
        """
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"])

        run()
        cmd = launched.launch[0][3]
        minted = cmd.split('--session-id "')[1].split('"')[0]
        assert minted != "cid-1"


class TestDegradedRestart:
    def test_gone_history_starts_fresh_and_says_so(self, store, launched, capsys):
        record(cwd=store.cwd, ids=["cid-1"])

        assert run() == 0
        cmd = launched.launch[0][3]
        # The RECORDED id is never resumed. (The line still carries a
        # `--resume <new-id>` branch — that is #901's re-entry case, keyed on
        # the id this launch mints, not on the dead one.)
        assert '--resume "cid-1"' not in cmd
        assert "--fork-session" not in cmd
        assert "--session-id" in cmd
        out = capsys.readouterr().out
        assert "No history found" in out and "role intact" in out




class TestRestartKeepsLaunchOverrides:
    def test_recorded_model_and_effort_survive_a_restart(self, store, launched):
        """#1198: the voice assistant's Haiku/medium choice lives in the
        launch record, so a restart must regenerate both flags from it."""
        from agentwire.core import (
            build_agent_command,
            load_session_metadata,
            record_session_launch,
        )

        agent = build_agent_command("bypass", model="claude-haiku-5-5", effort="medium")
        record_session_launch("sess", agent, store.cwd)
        meta = load_session_metadata("sess")
        assert (meta["model"], meta["effort"]) == ("claude-haiku-5-5", "medium")

        assert run() == 0
        cmd = launched.launch[0][3]
        assert "--model claude-haiku-5-5" in cmd and "--effort medium" in cmd

    def test_a_restart_regenerates_role_owned_hooks(self, store, launched):
        """The voice assistant's speech hooks ride --settings, derived from the
        recorded role — so a restart must bring them back."""
        record(cwd=store.cwd, roles=["voice-assistant"])
        assert run() == 0
        assert "-m agentwire.voice_hooks" in launched.launch[0][3]
