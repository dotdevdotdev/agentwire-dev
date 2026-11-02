"""Integration tests for CLI command handlers with mocked subprocess."""

import argparse
import json
from pathlib import Path
from unittest.mock import ANY, MagicMock

import yaml

# --- _recent_activity (scheduler status helper) ---



# --- cmd_roles_list ---



# --- cmd_safety_check ---

class TestCmdSafetyCheck:

    def test_blocked_by_pattern(self, tmp_path, monkeypatch):
        import agentwire.safety_commands as mod

        # The host's overlay dir (~/.agentwire/damage-control) adds rules to the bundled set.
        rules_dir = tmp_path / "damage-control"
        rules_dir.mkdir()
        patterns = {
            "bashToolPatterns": [
                {
                    "pattern": r"frobnicate\s+--everything",
                    "action": "block",
                    "reason": "Dangerous frobnication",
                }
            ]
        }
        with open(rules_dir / "patterns.yaml", "w") as f:
            yaml.safe_dump(patterns, f)

        monkeypatch.setenv("AGENTWIRE_DIR", str(tmp_path))

        result = mod.check_command_safety("frobnicate --everything")
        assert result["decision"] == "block"
        assert "Dangerous" in result["reason"]


# --- cmd_task_list / cmd_task_validate via tasks module ---

class TestTaskCommands:


    def test_validate_bad_task(self, project_dir):
        from agentwire.tasks import TaskConfig, validate_task

        task = TaskConfig(name="bad", prompt="ok", retries=-1, mode="invalid")
        issues = validate_task(task)
        assert len(issues) >= 2


# --- cmd_projects_list (via projects discovery) ---



# --- cmd_send --wait-ready ---

class TestCmdSendWaitReady:
    def _args(self, **overrides):
        defaults = dict(
            session="proj", pane=None, prompt=["my", "idea"],
            json=True, wait_ready=True, timeout=5.0,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def _payload(self, capsys):
        return json.loads(capsys.readouterr().out.strip())

    def _mock_has_session(self, monkeypatch):
        from agentwire import session_ready

        has_session = MagicMock(returncode=0)
        monkeypatch.setattr("agentwire.send_cli.subprocess.run", lambda *a, **k: has_session)
        # get_current_session shells out to real tmux and depends on the test
        # runner's own environment ($TMUX_PANE) -- pin it so fallback_sender
        # resolution is deterministic regardless of where tests run.
        monkeypatch.setattr("agentwire.send_cli.pane_manager.get_current_session", lambda: None)
        # #845 pre-flight: box is clear unless a test says otherwise (the real
        # one captures a live pane).
        monkeypatch.setattr(session_ready, "box_holds_foreign_draft", lambda *a, **k: False)

    def test_happy_path_verified(self, capsys, monkeypatch):
        from agentwire import session_ready
        from agentwire.send_cli import cmd_send

        self._mock_has_session(monkeypatch)
        monkeypatch.setattr(session_ready, "wait_for_session_ready", lambda s, timeout: True)
        monkeypatch.setattr(session_ready, "send_verified", lambda s, m, marker=None: True)

        assert cmd_send(self._args()) == 0
        payload = self._payload(capsys)
        assert payload["success"] is True
        assert payload["verified"] is True
        assert payload["fallback"] is None


    def test_foreign_draft_blocks_the_send_without_clearing(self, capsys, monkeypatch):
        """#845: a draft that was already in the box before we tried anything
        is not ours to paste over OR to erase -- queue and leave it alone."""
        from agentwire import session_ready
        from agentwire.send_cli import cmd_send

        self._mock_has_session(monkeypatch)
        monkeypatch.setattr(session_ready, "wait_for_session_ready", lambda s, timeout: True)
        monkeypatch.setattr(session_ready, "box_holds_foreign_draft", lambda *a, **k: True)
        sent = MagicMock()
        monkeypatch.setattr(session_ready, "send_verified", sent)
        cleared = MagicMock()
        monkeypatch.setattr(session_ready, "clear_input_box", cleared)
        seed = MagicMock(return_value="inbox_blocked")
        monkeypatch.setattr(session_ready, "recover_failed_seed", seed)

        assert cmd_send(self._args()) == 1
        payload = self._payload(capsys)
        assert payload["verified"] is False
        assert payload["fallback"] == "inbox_blocked"
        sent.assert_not_called()      # never pasted on top of the draft
        cleared.assert_not_called()   # and never erased it
        seed.assert_called_once_with(
            "proj", "my idea", sender="agentwire", clear=False)


    def test_unverified_falls_back_to_inbox(self, capsys, monkeypatch):
        """#834: an unverified send must never just hand the problem back to
        whichever caller reads the response — it queues to the durable msg
        inbox so delivery is retried and eventually dead-lettered LOUDLY
        instead of silently depending on the caller noticing and resending."""
        from agentwire import send_cli, session_ready
        from agentwire.send_cli import cmd_send

        self._mock_has_session(monkeypatch)
        monkeypatch.setattr(session_ready, "wait_for_session_ready", lambda s, timeout: True)
        monkeypatch.setattr(session_ready, "send_verified", lambda s, m, marker=None: False)
        recover = MagicMock(return_value="inbox")
        monkeypatch.setattr(send_cli, "_recover_unverified_send", recover)

        assert cmd_send(self._args()) == 1
        payload = self._payload(capsys)
        assert payload["verified"] is False
        assert payload["fallback"] == "inbox"
        recover.assert_called_once_with("proj", "my idea", "agentwire", marker=ANY)







# --- cmd_send --verify (no --wait-ready) ---

class TestCmdSendVerify:
    def _args(self, **overrides):
        defaults = dict(
            session="proj", pane=None, prompt=["my", "idea"],
            json=True, wait_ready=False, verify=True, timeout=None,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def _payload(self, capsys):
        return json.loads(capsys.readouterr().out.strip())

    def _mock_has_session(self, monkeypatch):
        from agentwire import session_ready

        has_session = MagicMock(returncode=0)
        monkeypatch.setattr("agentwire.send_cli.subprocess.run", lambda *a, **k: has_session)
        monkeypatch.setattr("agentwire.send_cli.pane_manager.get_current_session", lambda: None)
        monkeypatch.setattr(session_ready, "box_holds_foreign_draft", lambda *a, **k: False)


    def test_unverified_falls_back_to_inbox(self, capsys, monkeypatch):
        """#834: same durable-fallback guarantee as --wait-ready."""
        from agentwire import send_cli, session_ready
        from agentwire.send_cli import cmd_send

        self._mock_has_session(monkeypatch)
        monkeypatch.setattr(session_ready, "send_verified", lambda s, m, marker=None, retries=1: False)
        recover = MagicMock(return_value="inbox")
        monkeypatch.setattr(send_cli, "_recover_unverified_send", recover)

        assert cmd_send(self._args()) == 0  # verify-without-wait-ready reports success=True; verified carries the real state
        payload = self._payload(capsys)
        assert payload["verified"] is False
        assert payload["fallback"] == "inbox"
        recover.assert_called_once_with("proj", "my idea", "agentwire", marker=ANY)





class TestRecoverUnverifiedSend:
    """#835 review finding 2: the msg-inbox drain's dedup matches the
    WRAPPED `[MSG from ... ] ... <id>` render, never the bare text a direct
    paste puts on screen -- so blindly enqueuing risks a real duplicate
    delivery when the original send actually landed and only the confirm
    read was ambiguous. _recover_unverified_send closes the common case by
    checking scrollback for the bare message first."""




    def test_generic_prompt_on_scrollback_no_longer_swallows_a_failed_send(
            self, monkeypatch):
        """#839's false positive, end to end: a bare 'continue' from an
        UNRELATED earlier send sits in the scrollback window. Matching the bare
        text reports already_delivered and skips the inbox enqueue entirely --
        silently dropping a send that never landed. The marker makes the
        difference."""
        from agentwire import session_ready
        from agentwire.send_cli import _recover_unverified_send

        old_marker = session_ready.new_delivery_marker()
        this_marker = session_ready.new_delivery_marker()
        rule = "─" * 20
        cap = f"❯ continue  {old_marker}\n{rule}\n❯\n{rule}"
        monkeypatch.setattr(session_ready, "scrollback", lambda s, pane_index=0: cap)
        monkeypatch.setattr(
            session_ready, "recover_failed_seed", lambda *a, **k: "inbox")

        # Bare text (the pre-#839 behavior): the unrelated echo swallows it.
        assert _recover_unverified_send("proj", "continue", "agentwire") == "already_delivered"
        # Per-attempt marker: this send is correctly recognized as NOT delivered.
        assert _recover_unverified_send(
            "proj", "continue", "agentwire", marker=this_marker) == "inbox"


# --- cmd_new --first-message ---



class TestCmdNewSeedFallback:
    """#695 — cmd_new's JSON contract on a failed seed: recovery runs (clear
    box + msg-inbox fallback) and `first_message_fallback` tells the caller
    (mcp_worktree) which fallback fired, so the failure is never silent."""

    def _run_cmd_new(self, monkeypatch, tmp_path, *, ready, verified, fallback,
                     foreign_draft=False, on_scrollback=False):
        from types import SimpleNamespace

        from agentwire import session_cli as m
        from agentwire import session_ready

        # Hermetic stubs: no tmux, no roles from disk, no portal.
        monkeypatch.setattr(m, "_check_tmux_installed", lambda: True)
        monkeypatch.setattr(
            m.subprocess, "run", lambda *a, **k: MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(m, "load_config", lambda *a, **k: {})
        monkeypatch.setattr(m, "resolve_roles", lambda *a, **k: [])
        monkeypatch.setattr(m, "inject_soul", lambda names, cfg, no_soul=False: [])
        monkeypatch.setattr(
            m, "_resolve_posture_from_args", lambda a, **kw: ("bypass", None))
        monkeypatch.setattr(
            m, "build_agent_command",
            lambda *a, **k: SimpleNamespace(command="claude", env={}))
        monkeypatch.setattr(m, "_launch_tmux_session", lambda *a, **k: None)
        monkeypatch.setattr(m, "record_session_launch", lambda *a, **k: {})
        monkeypatch.setattr(m, "notify_portal_session_created", lambda *a, **k: None)
        monkeypatch.setattr(m, "_notify_portal_sessions_changed", lambda: None)

        calls = {}
        monkeypatch.setattr(
            session_ready, "wait_for_session_ready",
            lambda s, timeout=30.0, pane_index=0: ready)

        def fake_send_verified(session, msg, **k):
            calls["pasted"] = (msg, k.get("marker"))
            return verified

        monkeypatch.setattr(session_ready, "send_verified", fake_send_verified)
        # #845 pre-flight and #839 scrollback check both read the live pane —
        # stub the two primitives so the unit test stays hermetic.
        monkeypatch.setattr(
            session_ready, "box_holds_foreign_draft",
            lambda s, msg, **k: foreign_draft)
        monkeypatch.setattr(session_ready, "scrollback", lambda s, pane_index=0: "")
        monkeypatch.setattr(
            session_ready, "message_on_scrollback",
            lambda cap, rendered: on_scrollback)

        def fake_recover(session, message, sender=None, pane_index=0, clear=True):
            calls["recover"] = (session, message, sender)
            calls["recover_clear"] = clear
            return "inbox_blocked" if not clear else fallback

        monkeypatch.setattr(session_ready, "recover_failed_seed", fake_recover)

        args = argparse.Namespace(
            session="proj", path=str(tmp_path), force=False, json=True,
            first_message="do the thing", created_by="orch",
        )
        rc = m.cmd_new(args)
        return rc, calls

    def test_seed_failure_runs_recovery_and_reports_fallback(
            self, capsys, monkeypatch, tmp_path):
        rc, calls = self._run_cmd_new(
            monkeypatch, tmp_path, ready=False, verified=False, fallback="inbox")
        assert rc == 0  # the session exists; seeding failure doesn't fail the cmd
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["success"] is True
        assert payload["first_message_delivered"] is False
        assert payload["first_message_fallback"] == "inbox"
        # Recovery got the prompt and the creator as sender.
        assert calls["recover"] == ("proj", "do the thing", "orch")





class TestCmdNewSeedGuardedPath:
    """#840 — the seed goes through the SAME guarded send path as every other
    sender: a per-attempt marker rides inside the paste (#839), an ambiguous
    confirm consults scrollback before enqueuing a second copy, and a foreign
    draft in the box is left alone rather than clobbered (#845)."""

    _run_cmd_new = TestCmdNewSeedFallback._run_cmd_new




    def test_foreign_draft_is_queued_not_clobbered(self, capsys, monkeypatch, tmp_path):
        """#845 — boot can take the full 60s wait, ample room for a human to
        attach and start typing. Their draft is neither pasted over nor erased."""
        rc, calls = self._run_cmd_new(
            monkeypatch, tmp_path, ready=True, verified=True, fallback="inbox",
            foreign_draft=True)
        assert rc == 0
        assert "pasted" not in calls  # never pasted on top of the draft
        assert calls["recover_clear"] is False  # and never erased it either
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["first_message_delivered"] is False
        assert payload["first_message_fallback"] == "inbox_blocked"



class TestCmdNewWorktreeMissingDirFailsLoud:
    """#739 — `agentwire new --json` must never report success with a `path`
    that doesn't back a real worktree on disk. Two guards, two failure
    windows: (1) worktree creation reports ok but the dir never landed, (2)
    the dir existed right after creation but vanished before the pane
    actually launches."""

    def _base_args(self, project_path):
        return argparse.Namespace(
            session="proj/mybranch", path=str(project_path), force=False,
            json=True, base=None, pull_first=False, roles=None, no_soul=True,
        )

    def test_ensure_worktree_lies_about_success(self, capsys, monkeypatch, tmp_path):
        """Worktree creation reports ok without the dir existing (the #739
        symptom: `agentwire new` proceeded past worktree creation with a path
        whose directory was never actually created)."""
        from agentwire import session_cli as m

        project_path = tmp_path / "proj"
        project_path.mkdir()

        monkeypatch.setattr(m, "_check_tmux_installed", lambda: True)
        monkeypatch.setattr(
            m.subprocess, "run", lambda *a, **k: MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(m, "create_and_register_worktree", lambda *a, **k: (True, ""))
        monkeypatch.setattr(m, "load_config", lambda *a, **k: {})

        rc = m.cmd_new(self._base_args(project_path))
        assert rc == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["success"] is False
        assert "does not exist" in payload["error"]



class TestCmdNewDefaultCreatedByRooting:
    """#715 — with --created-by unset, cmd_new should only default to the
    caller when the new session is in the caller's own project; a genuinely
    different project gets its own standalone root instead of being flattened
    into the caller's subtree."""

    def _run(self, monkeypatch, tmp_path, *, caller_session, caller_project_path,
             kind=None, session="proj"):
        from types import SimpleNamespace

        from agentwire import core
        from agentwire import session_cli as m

        monkeypatch.setattr(m, "_check_tmux_installed", lambda: True)
        monkeypatch.setattr(
            m.subprocess, "run", lambda *a, **k: MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(m, "load_config", lambda *a, **k: {})
        monkeypatch.setattr(m, "resolve_roles", lambda *a, **k: [])
        monkeypatch.setattr(m, "inject_soul", lambda names, cfg, no_soul=False: [])
        monkeypatch.setattr(
            m, "_resolve_posture_from_args", lambda a, **kw: ("bypass", None))
        monkeypatch.setattr(
            m, "build_agent_command",
            lambda *a, **k: SimpleNamespace(command="claude", env={}))
        monkeypatch.setattr(m, "_launch_tmux_session", lambda *a, **k: None)
        monkeypatch.setattr(m, "_notify_portal_sessions_changed", lambda: None)
        monkeypatch.setattr(m, "notify_portal_session_created", lambda *a, **k: None)
        monkeypatch.setattr(m.pane_manager, "get_current_session", lambda: None)
        monkeypatch.setattr(core, "_live_session_cwd", lambda s: caller_project_path)

        recorded = {}

        def fake_record(session_name, agent, cwd, **kw):
            recorded["created_by"] = kw.get("created_by")
            return {}

        monkeypatch.setattr(m, "record_session_launch", fake_record)

        args = argparse.Namespace(
            session=session, path=str(tmp_path), force=False, json=True,
            created_by=None, caller_session=caller_session, kind=kind,
        )
        rc = m.cmd_new(args)
        assert rc == 0
        return recorded


    def test_cross_project_gets_standalone_root(self, monkeypatch, tmp_path):
        other_project = tmp_path.parent / "some-other-project"
        recorded = self._run(
            monkeypatch, tmp_path,
            caller_session="orchestrator", caller_project_path=other_project,
        )
        assert recorded["created_by"] is None








# --- cmd_recreate / cmd_fork route through resolve_roles (#311) ---
#
# Both commands used to copy `project_config.roles` raw, bypassing
# resolve_roles + the #309/#310 kind-derivation — so a recreated worktree
# session silently lost its non-overridable worker-worktree etiquette
# (isolation / verify / draft-PR / notify). These capture the role list each
# command hands to load_roles and assert the kind's intrinsic etiquette is
# present (or, for the orchestrator persona, replaceable).

class _RoleCapture:
    """Holds the role_names captured from a mocked load_roles call."""

    def __init__(self):
        self.role_names = None


def _patch_role_pipeline(monkeypatch, projects_dir, project_config_roles):
    """Mock out tmux/git/worktree side effects and capture resolved roles.

    Returns the capture object whose .role_names is the list cmd_recreate /
    cmd_fork pass to load_roles (i.e. resolve_roles + inject_soul output).
    """
    from types import SimpleNamespace

    import agentwire.session_cli as mod
    from agentwire.core import AgentCommand

    cap = _RoleCapture()

    cfg = None
    if project_config_roles is not None:
        cfg = SimpleNamespace(
            posture="bypass",
            roles=project_config_roles,
        )

    monkeypatch.setattr(mod, "load_config", lambda: {
        "projects": {"dir": str(projects_dir), "worktrees": {"suffix": "-worktrees"}},
    })
    monkeypatch.setattr(mod, "load_project_config", lambda p: cfg)
    monkeypatch.setattr(mod, "build_agent_command", lambda *a, **k: AgentCommand(command=""))

    def fake_create_worktree(project, *, worktree_path, **kw):
        Path(worktree_path).mkdir(parents=True, exist_ok=True)
        return True, ""

    monkeypatch.setattr(mod, "create_and_register_worktree", fake_create_worktree)

    def fake_load_roles(role_names, path):
        cap.role_names = list(role_names)
        return [], []

    monkeypatch.setattr(mod, "load_roles", fake_load_roles)
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)
    return cap


def _fake_run(source_session=None, cwd=None):
    """Command-aware tmux/git stub.

    has-session: source exists (rc 0), everything else absent (rc 1) so
    recreate skips its kill path and a non-worktree fork sees its target free.
    """
    def run(cmd, *a, **k):
        joined = " ".join(str(x) for x in (cmd if isinstance(cmd, list) else [cmd]))
        if "has-session" in joined:
            if source_session and source_session in joined:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="")
        if "display-message" in joined:
            if "pane_current_path" in joined:
                return MagicMock(returncode=0, stdout=f"{cwd or ''}\n", stderr="")
            return MagicMock(returncode=0, stdout="0\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    return run


class TestRecreateRoutesThroughResolveRoles:
    def test_worktree_recreate_reinjects_etiquette_even_without_saved_roles(
        self, monkeypatch, tmp_path
    ):
        import agentwire.session_cli as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=None)
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(
            session="proj/feature", json=True, posture=None, env=None,
        )
        assert mod.cmd_recreate(args) == 0
        # The whole point: a project/branch recreate is a worker on worktree
        # topology, so the safety contract is present even though nothing
        # was saved.
        assert cap.role_names[0] == "worker-worktree"
        assert "soul" in cap.role_names







# --- cmd_history_resume routes through resolve_roles (#316) ---
#
# history-resume used to copy `project_config.roles` raw, bypassing
# resolve_roles + kind-derivation — so a zero-config resume got an empty role
# list instead of the orchestrator etiquette a fresh `agentwire new` would.
# A history-resume has no branch, so its kind is always "orchestrator".



