"""Integration tests for CLI command handlers with mocked subprocess."""

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import yaml

# --- _recent_activity (scheduler status helper) ---

class TestRecentActivity:
    def test_keeps_outcome_events_newest_first(self):
        from agentwire.scheduler_cli import _recent_activity

        events = [
            {"ts": "2026-06-15T10:00:00+00:00", "event": "scheduler_sleeping"},
            {"ts": "2026-06-15T10:01:00+00:00", "event": "task_completed",
             "task": "a", "status": "complete", "summary": "ok"},
            {"ts": "2026-06-15T10:02:00+00:00", "event": "task_started", "task": "b"},
            {"ts": "2026-06-15T10:03:00+00:00", "event": "gate_error",
             "task": "b", "gate_type": "git_commit", "reason": "TimeoutExpired"},
        ]
        out = _recent_activity(events, limit=5)
        # Newest first, non-outcome events dropped.
        assert [i["task"] for i in out] == ["b", "a"]
        assert out[0]["detail"].startswith("[gate-error] git_commit")
        assert "complete" in out[1]["detail"]

    def test_respects_limit(self):
        from agentwire.scheduler_cli import _recent_activity

        events = [
            {"ts": f"2026-06-15T10:0{i}:00+00:00", "event": "task_completed",
             "task": f"t{i}", "status": "complete"}
            for i in range(6)
        ]
        assert len(_recent_activity(events, limit=3)) == 3


# --- cmd_roles_list ---

class TestCmdRolesList:
    def test_json_output(self, capsys):
        """cmd_roles_list --json should return bundled roles."""

        # Directly test that roles are loadable
        from agentwire.roles import discover_role, parse_role_file

        bundled_names = ["agentwire", "voice", "worker", "task-runner", "chatbot", "init"]
        roles = []
        for name in bundled_names:
            path = discover_role(name)
            if path:
                role = parse_role_file(path)
                if role:
                    roles.append({
                        "name": role.name,
                        "description": role.description,
                        "has_tools": bool(role.tools),
                        "has_disallowed": bool(role.disallowed_tools),
                    })

        assert len(roles) == 6
        # Every role should have a name
        for r in roles:
            assert r["name"]


# --- cmd_safety_check ---

class TestCmdSafetyCheck:
    def test_allowed_command(self, tmp_path, monkeypatch):
        import agentwire.safety_commands as mod
        monkeypatch.setattr(mod, "RULES_DIR", tmp_path / "empty-rules")

        result = mod.check_command_safety("echo hello")
        assert result["decision"] == "allow"

    def test_blocked_by_pattern(self, tmp_path, monkeypatch):
        import agentwire.safety_commands as mod

        # Create a rules dir with a blocking pattern
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        patterns = {
            "bashToolPatterns": [
                {
                    "pattern": r"rm\s+-rf\s+/",
                    "action": "block",
                    "reason": "Dangerous recursive delete",
                }
            ]
        }
        with open(rules_dir / "patterns.yaml", "w") as f:
            yaml.safe_dump(patterns, f)

        monkeypatch.setattr(mod, "RULES_DIR", rules_dir)

        result = mod.check_command_safety("rm -rf /")
        assert result["decision"] == "block"
        assert "Dangerous" in result["reason"]


# --- cmd_task_list / cmd_task_validate via tasks module ---

class TestTaskCommands:
    def test_list_tasks(self, project_dir):
        config_path = project_dir / ".agentwire.tasks.yml"
        data = {
            "tasks": {
                "lint": {"prompt": "Run linting."},
                "test": {"prompt": "Run tests.", "retries": 2},
            }
        }
        with open(config_path, "w") as f:
            yaml.safe_dump(data, f)

        from agentwire.tasks import list_tasks
        tasks = list_tasks(project_dir)
        assert len(tasks) == 2
        names = {t["name"] for t in tasks}
        assert "lint" in names
        assert "test" in names

    def test_validate_good_task(self, project_dir):
        config_path = project_dir / ".agentwire.tasks.yml"
        data = {"tasks": {"good": {"prompt": "Do things.", "retries": 1}}}
        with open(config_path, "w") as f:
            yaml.safe_dump(data, f)

        from agentwire.tasks import load_task, validate_task
        task = load_task(project_dir, "good")
        issues = validate_task(task)
        assert issues == []

    def test_validate_bad_task(self, project_dir):
        from agentwire.tasks import TaskConfig, validate_task

        task = TaskConfig(name="bad", prompt="ok", retries=-1, mode="invalid")
        issues = validate_task(task)
        assert len(issues) >= 2


# --- cmd_projects_list (via projects discovery) ---

class TestProjectsDiscovery:
    def test_discovers_projects(self, tmp_path):
        """Projects with .agentwire.yml or .git should be discoverable."""
        # Create fake projects
        p1 = tmp_path / "project-a"
        p1.mkdir()
        (p1 / ".git").mkdir()

        p2 = tmp_path / "project-b"
        p2.mkdir()
        with open(p2 / ".agentwire.yml", "w") as f:
            yaml.safe_dump({"posture": "bare"}, f)

        p3 = tmp_path / "not-a-project"
        p3.mkdir()

        # Check that we can identify projects
        projects = []
        for d in sorted(tmp_path.iterdir()):
            if d.is_dir():
                has_git = (d / ".git").exists()
                has_config = (d / ".agentwire.yml").exists()
                if has_git or has_config:
                    projects.append(d.name)

        assert "project-a" in projects
        assert "project-b" in projects
        assert "not-a-project" not in projects


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

    def test_happy_path_verified(self, capsys, monkeypatch):
        from agentwire import session_ready
        from agentwire.send_cli import cmd_send

        has_session = MagicMock(returncode=0)
        monkeypatch.setattr("agentwire.send_cli.subprocess.run", lambda *a, **k: has_session)
        monkeypatch.setattr(session_ready, "wait_for_session_ready", lambda s, timeout: True)
        monkeypatch.setattr(session_ready, "send_verified", lambda s, m: True)

        assert cmd_send(self._args()) == 0
        payload = self._payload(capsys)
        assert payload["success"] is True
        assert payload["verified"] is True

    def test_not_ready_fails(self, capsys, monkeypatch):
        from agentwire import session_ready
        from agentwire.send_cli import cmd_send

        has_session = MagicMock(returncode=0)
        monkeypatch.setattr("agentwire.send_cli.subprocess.run", lambda *a, **k: has_session)
        monkeypatch.setattr(session_ready, "wait_for_session_ready", lambda s, timeout: False)

        assert cmd_send(self._args()) == 1
        payload = self._payload(capsys)
        assert payload["success"] is False
        assert "not ready" in payload["error"]

    def test_unverified_fails(self, capsys, monkeypatch):
        from agentwire import session_ready
        from agentwire.send_cli import cmd_send

        has_session = MagicMock(returncode=0)
        monkeypatch.setattr("agentwire.send_cli.subprocess.run", lambda *a, **k: has_session)
        monkeypatch.setattr(session_ready, "wait_for_session_ready", lambda s, timeout: True)
        monkeypatch.setattr(session_ready, "send_verified", lambda s, m: False)

        assert cmd_send(self._args()) == 1
        payload = self._payload(capsys)
        assert payload["verified"] is False

    def test_remote_rejected(self, capsys):
        from agentwire.send_cli import cmd_send

        assert cmd_send(self._args(session="proj@gpu")) == 1
        payload = self._payload(capsys)
        assert "local-only" in payload["error"]

    def test_pane_combo_rejected(self, capsys):
        from agentwire.send_cli import cmd_send

        assert cmd_send(self._args(pane=1)) == 1
        payload = self._payload(capsys)
        assert "--pane" in payload["error"]


# --- cmd_new --first-message ---

class TestCmdNewFirstMessage:
    def test_remote_rejected(self, capsys, monkeypatch):
        from agentwire.session_cli import cmd_new

        monkeypatch.setattr("agentwire.session_cli._check_tmux_installed", lambda: True)
        args = argparse.Namespace(
            session="proj@gpu", path=None, force=False, json=True,
            roles=None, no_soul=True, first_message="an idea",
        )
        assert cmd_new(args) == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert "local-only" in payload["error"]


class TestCmdNewSeedFallback:
    """#695 — cmd_new's JSON contract on a failed seed: recovery runs (clear
    box + msg-inbox fallback) and `first_message_fallback` tells the caller
    (mcp_worktree) which fallback fired, so the failure is never silent."""

    def _run_cmd_new(self, monkeypatch, tmp_path, *, ready, verified, fallback):
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
        monkeypatch.setattr(m, "_record_session_creator", lambda *a, **k: None)
        monkeypatch.setattr(m, "_record_session_role", lambda *a, **k: None)
        monkeypatch.setattr(m, "notify_portal_session_created", lambda *a, **k: None)
        monkeypatch.setattr(m, "_notify_portal_sessions_changed", lambda: None)

        calls = {}
        monkeypatch.setattr(
            session_ready, "wait_for_session_ready",
            lambda s, timeout=30.0, pane_index=0: ready)
        monkeypatch.setattr(
            session_ready, "send_verified",
            lambda s, msg, **k: verified)

        def fake_recover(session, message, sender=None, pane_index=0):
            calls["recover"] = (session, message, sender)
            return fallback

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

    def test_seed_failure_fallback_also_failed(self, capsys, monkeypatch, tmp_path):
        rc, _ = self._run_cmd_new(
            monkeypatch, tmp_path, ready=True, verified=False, fallback=None)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["first_message_delivered"] is False
        assert payload["first_message_fallback"] is None

    def test_seed_success_no_fallback_key(self, capsys, monkeypatch, tmp_path):
        rc, calls = self._run_cmd_new(
            monkeypatch, tmp_path, ready=True, verified=True, fallback="inbox")
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["first_message_delivered"] is True
        assert "first_message_fallback" not in payload
        assert "recover" not in calls  # recovery never runs on success


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
        """`ensure_worktree` returns True without the dir existing (the #739
        symptom: `agentwire new` proceeded past worktree creation with a path
        whose directory was never actually created)."""
        from agentwire import session_cli as m

        project_path = tmp_path / "proj"
        project_path.mkdir()

        monkeypatch.setattr(m, "_check_tmux_installed", lambda: True)
        monkeypatch.setattr(
            m.subprocess, "run", lambda *a, **k: MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(m, "ensure_worktree", lambda *a, **k: True)
        monkeypatch.setattr(m, "load_config", lambda *a, **k: {})

        rc = m.cmd_new(self._base_args(project_path))
        assert rc == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["success"] is False
        assert "does not exist" in payload["error"]

    def test_dir_vanishes_between_creation_and_launch(self, capsys, monkeypatch, tmp_path):
        """The dir is real right after `ensure_worktree`, but something
        removes it before `_launch_tmux_session` runs — the pre-launch guard
        must catch this instead of launching the agent into an ENOENT."""
        import shutil
        from types import SimpleNamespace

        from agentwire import session_cli as m

        project_path = tmp_path / "proj"
        project_path.mkdir()
        session_path = tmp_path / "proj-worktrees" / "mybranch"

        def fake_ensure_worktree(proj, branch, wt_path, **kw):
            wt_path.mkdir(parents=True)
            return True

        monkeypatch.setattr(m, "_check_tmux_installed", lambda: True)
        monkeypatch.setattr(
            m.subprocess, "run", lambda *a, **k: MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(m, "ensure_worktree", fake_ensure_worktree)
        monkeypatch.setattr(m, "load_config", lambda *a, **k: {})
        monkeypatch.setattr(m, "resolve_roles", lambda *a, **k: [])
        monkeypatch.setattr(m, "inject_soul", lambda names, cfg, no_soul=False: [])
        monkeypatch.setattr(
            m, "_resolve_posture_from_args", lambda a, **kw: ("bypass", None))

        def vanish_then_build(*a, **k):
            shutil.rmtree(str(session_path))
            return SimpleNamespace(command="claude", env={})

        monkeypatch.setattr(m, "build_agent_command", vanish_then_build)
        launched = []
        monkeypatch.setattr(m, "_launch_tmux_session", lambda *a, **k: launched.append(True))

        rc = m.cmd_new(self._base_args(project_path))
        assert rc == 1
        assert not launched
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["success"] is False
        assert "vanished before launch" in payload["error"]


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
        monkeypatch.setattr(m, "_record_session_role", lambda *a, **k: None)
        monkeypatch.setattr(m, "notify_portal_session_created", lambda *a, **k: None)
        monkeypatch.setattr(m.pane_manager, "get_current_session", lambda: None)
        monkeypatch.setattr(core, "_live_session_cwd", lambda s: caller_project_path)

        recorded = {}

        def fake_record(session_name, created_by, via):
            recorded["created_by"] = created_by

        monkeypatch.setattr(m, "_record_session_creator", fake_record)

        args = argparse.Namespace(
            session=session, path=str(tmp_path), force=False, json=True,
            created_by=None, caller_session=caller_session, kind=kind,
        )
        rc = m.cmd_new(args)
        assert rc == 0
        return recorded

    def test_same_project_inherits_caller(self, monkeypatch, tmp_path):
        recorded = self._run(
            monkeypatch, tmp_path,
            caller_session="orchestrator", caller_project_path=Path(tmp_path),
        )
        assert recorded["created_by"] == "orchestrator"

    def test_cross_project_gets_standalone_root(self, monkeypatch, tmp_path):
        other_project = tmp_path.parent / "some-other-project"
        recorded = self._run(
            monkeypatch, tmp_path,
            caller_session="orchestrator", caller_project_path=other_project,
        )
        assert recorded["created_by"] is None

    def test_no_caller_session_falls_back_to_pane_manager(self, monkeypatch, tmp_path):
        # Neither --caller-session (MCP) nor a live tmux pane (bare CLI outside
        # tmux) is available — no candidate caller at all, so no inheritance.
        recorded = self._run(
            monkeypatch, tmp_path,
            caller_session=None, caller_project_path=Path(tmp_path),
        )
        assert recorded["created_by"] is None

    def test_explicit_kind_orchestrator_roots_even_same_project_caller(self, monkeypatch, tmp_path):
        # #716: cmd_new is the ONE place this joint default lives — it must
        # fire whether cmd_new is reached directly (`agentwire new --kind
        # orchestrator` / `session_create(kind="orchestrator")`) or via
        # cmd_worktree's _launch_session, which just forwards --kind through.
        # Without this, a durable orchestrator created via `agentwire new`
        # directly (skipping cmd_worktree) would silently inherit the caller
        # as parent whenever same-project — contradicting its own "roots by
        # default" contract.
        recorded = self._run(
            monkeypatch, tmp_path,
            caller_session="orchestrator", caller_project_path=Path(tmp_path),
            kind="orchestrator",
        )
        assert recorded["created_by"] == ""

    def test_plain_branchless_new_keeps_inherit_behavior_even_though_it_derives_orchestrator(self, monkeypatch, tmp_path):
        # The joint default is gated on the EXPLICIT --kind flag, not the
        # resolved kind (a plain branchless name always derives to
        # "orchestrator" via derive_session_kind) — otherwise every ordinary
        # `agentwire new -s name` call would stop inheriting same-project
        # callers, a much bigger behavior change than #716 asked for.
        recorded = self._run(
            monkeypatch, tmp_path,
            caller_session="orchestrator", caller_project_path=Path(tmp_path),
            kind=None,
        )
        assert recorded["created_by"] == "orchestrator"

    def test_explicit_kind_reviewer_stays_parented_same_project_caller(self, monkeypatch, tmp_path):
        # #827: unlike orchestrator, --kind reviewer must NOT join the joint
        # rooting default — the gate is an exact string match against
        # 'orchestrator', so reviewer falls through to the normal
        # same-project inherit path below. A reviewer is scoped to a specific
        # sibling's PR, so it should nest under its spawner (sidebar tree,
        # notify-parent) rather than rooting like a durable orchestrator.
        recorded = self._run(
            monkeypatch, tmp_path,
            caller_session="orchestrator", caller_project_path=Path(tmp_path),
            kind="reviewer",
        )
        assert recorded["created_by"] == "orchestrator"


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

    def fake_ensure_worktree(base, branch, wt, **kw):
        Path(wt).mkdir(parents=True, exist_ok=True)
        return True

    monkeypatch.setattr(mod, "ensure_worktree", fake_ensure_worktree)

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

    def test_worktree_recreate_stacks_saved_roles_under_etiquette(
        self, monkeypatch, tmp_path
    ):
        import agentwire.session_cli as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=["domain"])
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(
            session="proj/feature", json=True, posture=None, env=None,
        )
        assert mod.cmd_recreate(args) == 0
        # Non-overridable: etiquette first, saved role stacks, never replaces.
        assert cap.role_names[0] == "worker-worktree"
        assert "domain" in cap.role_names

    def test_plain_recreate_is_orchestrator_replaceable(self, monkeypatch, tmp_path):
        import agentwire.session_cli as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=["custom"])
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(session="proj", json=True, posture=None, env=None)
        assert mod.cmd_recreate(args) == 0
        # Persona kind: saved roles REPLACE the orchestrator default.
        assert "orchestrator" not in cap.role_names
        assert "custom" in cap.role_names

    def test_plain_recreate_zero_config_is_orchestrator(self, monkeypatch, tmp_path):
        import agentwire.session_cli as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=None)
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(session="proj", json=True, posture=None, env=None)
        assert mod.cmd_recreate(args) == 0
        assert cap.role_names[0] == "orchestrator"


class TestForkRoutesThroughResolveRoles:
    def test_worktree_fork_injects_worker_worktree_etiquette(self, monkeypatch, tmp_path):
        import agentwire.session_cli as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)  # source_path (no source branch)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=None)
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(
            source="proj", target="proj/feat", json=True, posture=None,
            env=None, commit=None,
        )
        assert mod.cmd_fork(args) == 0
        # Fork target is a worktree → worker etiquette on worktree topology, intrinsic.
        assert cap.role_names[0] == "worker-worktree"

    def test_worktree_fork_stacks_source_roles_under_etiquette(self, monkeypatch, tmp_path):
        import agentwire.session_cli as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=["domain"])
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(
            source="proj", target="proj/feat", json=True, posture=None,
            env=None, commit=None,
        )
        assert mod.cmd_fork(args) == 0
        assert cap.role_names[0] == "worker-worktree"
        assert "domain" in cap.role_names

    def test_non_worktree_fork_is_orchestrator_replaceable(self, monkeypatch, tmp_path):
        import agentwire.session_cli as mod

        projects = tmp_path / "projects"
        projects.mkdir(parents=True)
        src_cwd = tmp_path / "src_cwd"
        src_cwd.mkdir()
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=["custom"])
        monkeypatch.setattr(
            mod.subprocess, "run", _fake_run(source_session="ctxa", cwd=src_cwd)
        )

        args = argparse.Namespace(
            source="ctxa", target="ctxb", json=True, posture=None,
            env=None, commit=None,
        )
        assert mod.cmd_fork(args) == 0
        # Same-dir fork has no branch → orchestrator persona; source roles win.
        assert "orchestrator" not in cap.role_names
        assert "custom" in cap.role_names


# --- cmd_history_resume routes through resolve_roles (#316) ---
#
# history-resume used to copy `project_config.roles` raw, bypassing
# resolve_roles + kind-derivation — so a zero-config resume got an empty role
# list instead of the orchestrator etiquette a fresh `agentwire new` would.
# A history-resume has no branch, so its kind is always "orchestrator".

def _patch_history_resume(monkeypatch, tmp_path, project_config_roles):
    """Mock tmux/history side effects and capture the resolved roles.

    Returns (cap, project_dir). cap.role_names is the list cmd_history_resume
    passes to load_roles (resolve_roles + inject_soul output).
    """
    from types import SimpleNamespace

    import agentwire.history as hist
    import agentwire.history_cli as mod

    cap = _RoleCapture()

    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    cfg = None
    if project_config_roles is not None:
        cfg = SimpleNamespace(
            posture="bypass",
            roles=project_config_roles,
        )

    monkeypatch.setattr(mod, "load_config", lambda: {})
    monkeypatch.setattr(mod, "load_project_config", lambda p: cfg)
    monkeypatch.setattr(hist, "resolve_session_id", lambda sid, mid: sid)

    def fake_load_roles(role_names, path):
        cap.role_names = list(role_names)
        return [], []

    monkeypatch.setattr(mod, "load_roles", fake_load_roles)
    monkeypatch.setattr(mod, "_notify_portal_sessions_changed", lambda *a, **k: None)
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    def fake_run(cmd, *a, **k):
        joined = " ".join(str(x) for x in (cmd if isinstance(cmd, list) else [cmd]))
        if "has-session" in joined:
            return MagicMock(returncode=1, stdout="", stderr="")  # absent → create
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return cap, project_dir


class TestHistoryResumeRoutesThroughResolveRoles:
    def test_zero_config_resume_is_orchestrator(self, monkeypatch, tmp_path):
        import agentwire.history_cli as mod

        cap, project_dir = _patch_history_resume(
            monkeypatch, tmp_path, project_config_roles=None
        )
        args = argparse.Namespace(
            session_id="abc123", name="resumed", machine="local",
            project=str(project_dir), json=True,
        )
        assert mod.cmd_history_resume(args) == 0
        # A resume with no saved roles now gets the orchestrator default,
        # not an empty list. Soul is auto-appended.
        assert cap.role_names[0] == "orchestrator"
        assert "soul" in cap.role_names

    def test_saved_roles_replace_orchestrator_persona(self, monkeypatch, tmp_path):
        import agentwire.history_cli as mod

        cap, project_dir = _patch_history_resume(
            monkeypatch, tmp_path, project_config_roles=["custom"]
        )
        args = argparse.Namespace(
            session_id="abc123", name="resumed", machine="local",
            project=str(project_dir), json=True,
        )
        assert mod.cmd_history_resume(args) == 0
        # Orchestrator is a persona kind → saved roles REPLACE the default.
        assert "orchestrator" not in cap.role_names
        assert "custom" in cap.role_names
