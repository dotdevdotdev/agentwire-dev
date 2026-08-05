"""Tests for ``agentwire restart`` — relaunch in place, same conversation (#871).

The behaviours worth pinning down are the ones a plausible implementation gets
wrong: re-evaluating the stored launch line (whose ``--session-id`` is
single-use), assuming a recorded id is resumable, and killing the session the
command itself runs in.
"""

import types
from pathlib import Path

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
    monkeypatch.setattr("agentwire.history.PROJECTS_DIR", tmp_path / "projects")
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


class TestResolveResumeTarget:
    def test_picks_the_newest_with_history(self, store):
        write_history(store, store.cwd, "old")
        write_history(store, store.cwd, "new")
        rid, loc = restart_cli.resolve_resume_target(["old", "new"], str(store.cwd))
        assert rid == "new"
        assert loc.status == "resumable"

    def test_falls_back_down_the_chain_for_a_never_prompted_launch(self, store):
        """The newest id has no ``.jsonl`` until the session's first turn.

        A relaunched-but-unspoken-to session must resume the id it forked
        FROM, not start blank.
        """
        write_history(store, store.cwd, "older")
        rid, _ = restart_cli.resolve_resume_target(["older", "never-prompted"],
                                                   str(store.cwd))
        assert rid == "older"

    def test_orphaned_history_is_not_resumable(self, store):
        moved = store.root / "elsewhere"
        moved.mkdir()
        write_history(store, moved, "cid")
        rid, loc = restart_cli.resolve_resume_target(["cid"], str(store.cwd))
        assert rid is None
        assert loc.status == "orphaned"
        assert loc.elsewhere

    def test_no_history_anywhere_is_gone(self, store):
        rid, loc = restart_cli.resolve_resume_target(["cid"], str(store.cwd))
        assert rid is None
        assert loc.status == "gone"

    def test_orphaned_outranks_gone_when_reporting(self, store):
        """A never-prompted newest id must not hide a recoverable orphan.

        Chain tail is ``gone`` (launched, never spoken to), but an earlier
        conversation is sitting under a stale key — that's the one finding
        anyone can act on, so it's the one reported.
        """
        moved = store.root / "elsewhere"
        moved.mkdir()
        write_history(store, moved, "older")
        rid, loc = restart_cli.resolve_resume_target(
            ["older", "never-prompted"], str(store.cwd)
        )
        assert rid is None
        assert loc.status == "orphaned" and loc.conversation_id == "older"

    def test_empty_chain(self, store):
        assert restart_cli.resolve_resume_target([], str(store.cwd)) == (None, None)


class TestRestartLaunch:
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

    def test_regenerates_flags_rather_than_replaying_a_stored_launch_line(
        self, store, launched
    ):
        """A stale recorded launch line must not leak into the relaunch."""
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"], posture="auto",
               launch_cmd="claude --session-id cid-1 --dangerously-skip-permissions")

        run()
        cmd = launched.launch[0][3]
        assert "--enable-auto-mode" in cmd  # regenerated from the recorded posture
        assert "--dangerously-skip-permissions" not in cmd

    def test_carries_the_recorded_model_override(self, store, launched):
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"], model="haiku")

        run()
        assert "--model haiku" in launched.launch[0][3]

    def test_appends_the_new_conversation_to_the_chain(self, store, launched):
        from agentwire.core import load_session_metadata

        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"], created_by="orch", role="worker")

        run()
        meta = load_session_metadata("sess")
        assert meta["conversation_ids"][0] == "cid-1"
        assert len(meta["conversation_ids"]) == 2
        # A restart is not a creation: parentage and role survive untouched.
        assert meta["created_by"] == "orch"
        assert meta["role"] == "worker"

    def test_relaunches_a_session_that_is_not_running(self, store, launched):
        """Post-reboot is the motivating case: nothing to /exit, still restartable."""
        launched.live = False
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"])

        assert run() == 0
        assert launched.killed == []
        assert launched.launch

    def test_stamps_session_env_for_the_launch_guard(self, store, launched):
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"], created_by="orch")

        run()
        env = launched.launch[0][2]
        assert env["AGENTWIRE_SESSION_NAME"] == "sess"
        assert env["AGENTWIRE_CREATED_BY"] == "orch"

    def test_bare_posture_launches_no_agent_and_skips_the_wait(self, store, launched, monkeypatch, capsys):
        monkeypatch.setattr(
            "agentwire.session_ready.wait_for_session_ready",
            lambda *a, **k: pytest.fail("bare has no agent to wait for"),
        )
        record(cwd=store.cwd, posture="bare")

        assert run() == 0
        assert launched.launch[0][3] == ""
        # No agent means no conversation — a history note here would be about
        # something a bare pane cannot have.
        out = capsys.readouterr().out
        assert "conversation" not in out.lower() and "role intact" not in out


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

    def test_orphaned_history_names_where_it_went(self, store, launched, capsys):
        moved = store.root / "elsewhere"
        moved.mkdir()
        write_history(store, moved, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"])

        assert run() == 0
        cmd = launched.launch[0][3]
        assert '--resume "cid-1"' not in cmd and "--fork-session" not in cmd
        out = capsys.readouterr().out
        assert "ORPHANED" in out

    def test_json_reports_the_degradation(self, store, launched, capsys):
        import json

        record(cwd=store.cwd, ids=["cid-1"])
        args = types.SimpleNamespace(session="sess", json=True, no_wait=False)
        assert restart_cli.cmd_restart(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["resumed_from"] is None
        assert payload["history_state"] == "gone"
        assert payload["note"]

    def test_missing_role_warns_but_still_restarts(self, store, launched, capsys):
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"], roles=["no-such-role-xyz"])

        assert run() == 0
        assert "no-such-role-xyz" in capsys.readouterr().err

    def test_unverified_relaunch_exits_nonzero(self, store, launched):
        launched.ready = False
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"])

        assert run() == 1
        assert launched.launch  # it did relaunch; the claim is just not verified

    def test_no_wait_skips_verification(self, store, launched, monkeypatch):
        monkeypatch.setattr(
            "agentwire.session_ready.wait_for_session_ready",
            lambda *a, **k: pytest.fail("--no-wait must not poll"),
        )
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"])

        args = types.SimpleNamespace(session="sess", json=False, no_wait=True)
        assert restart_cli.cmd_restart(args) == 0


class TestRestartRefusals:
    def test_no_record(self, store, launched, capsys):
        assert run("never-launched") == 1
        assert "No launch record" in capsys.readouterr().err
        assert not launched.launch

    def test_record_without_a_cwd_is_not_restartable(self, store, launched, capsys):
        from agentwire.core import store_session_metadata

        store_session_metadata("sess", {"created_by": "orch"})  # pre-#871 shape
        assert run() == 1
        assert not launched.launch

    def test_killed_session_is_finished(self, store, launched, capsys):
        """`agentwire kill` unlinks the record on purpose (stale-parent reuse),
        so a killed session has nothing to regenerate from — say that, rather
        than only blaming a pre-#871 launch."""
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"])
        (store.root / "agentwire" / "sessions" / "sess" / "metadata.json").unlink()

        assert run() == 1
        assert "already killed" in capsys.readouterr().err
        assert not launched.launch

    def test_missing_working_directory(self, store, launched, capsys):
        record(cwd=store.root / "gone", ids=["cid-1"])
        assert run() == 1
        assert "Recorded working directory is gone" in capsys.readouterr().err
        assert not launched.launch

    def test_refuses_to_restart_itself(self, store, launched, monkeypatch, capsys):
        monkeypatch.setattr(restart_cli.pane_manager, "get_current_session",
                            lambda: "sess")
        write_history(store, store.cwd, "cid-1")
        record(cwd=store.cwd, ids=["cid-1"])

        assert run() == 1
        assert "running in" in capsys.readouterr().err
        assert launched.killed == [] and not launched.launch

    def test_remote_session_refused(self, store, launched, capsys):
        assert run("sess@jade") == 1
        assert "local-only" in capsys.readouterr().err
        assert not launched.launch

    def test_missing_session_arg(self, store, launched):
        assert run(None) == 1
        assert not launched.launch

    def test_session_name_is_tmux_sanitized_before_lookup(self, store, launched):
        """tmux rewrites `.`/`:` to `_`, so the record is keyed by the safe name."""
        write_history(store, store.cwd, "cid-1")
        record("proj_x", cwd=store.cwd, ids=["cid-1"])

        assert run("proj.x") == 0
        assert launched.launch[0][0] == "proj_x"


class TestParser:
    def test_registered(self):
        from agentwire.__main__ import build_parser

        args = build_parser().parse_args(["restart", "-s", "x", "--json"])
        assert args.func is restart_cli.cmd_restart
        assert args.session == "x" and args.json is True

    def test_path_never_string_built(self):
        """restart launches at the RECORDED cwd, never a rebuilt convention path."""
        source = Path(restart_cli.__file__).read_text()
        assert "worktrees/" not in source
