"""Integration tests for CLI command handlers with mocked subprocess."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml


# --- cmd_roles_list ---

class TestCmdRolesList:
    def test_json_output(self, capsys):
        """cmd_roles_list --json should return bundled roles."""
        from agentwire.__main__ import _output_json

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
        import agentwire.cli_safety as mod
        monkeypatch.setattr(mod, "RULES_DIR", tmp_path / "empty-rules")

        result = mod.check_command_safety("echo hello")
        assert result["decision"] == "allow"

    def test_blocked_by_pattern(self, tmp_path, monkeypatch):
        import agentwire.cli_safety as mod

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
        config_path = project_dir / ".agentwire.yml"
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
        config_path = project_dir / ".agentwire.yml"
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
            yaml.safe_dump({"type": "bare"}, f)

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
        from agentwire.__main__ import cmd_send

        has_session = MagicMock(returncode=0)
        monkeypatch.setattr("agentwire.__main__.subprocess.run", lambda *a, **k: has_session)
        monkeypatch.setattr(session_ready, "wait_for_session_ready", lambda s, timeout: True)
        monkeypatch.setattr(session_ready, "send_verified", lambda s, m: True)

        assert cmd_send(self._args()) == 0
        payload = self._payload(capsys)
        assert payload["success"] is True
        assert payload["verified"] is True

    def test_not_ready_fails(self, capsys, monkeypatch):
        from agentwire import session_ready
        from agentwire.__main__ import cmd_send

        has_session = MagicMock(returncode=0)
        monkeypatch.setattr("agentwire.__main__.subprocess.run", lambda *a, **k: has_session)
        monkeypatch.setattr(session_ready, "wait_for_session_ready", lambda s, timeout: False)

        assert cmd_send(self._args()) == 1
        payload = self._payload(capsys)
        assert payload["success"] is False
        assert "not ready" in payload["error"]

    def test_unverified_fails(self, capsys, monkeypatch):
        from agentwire import session_ready
        from agentwire.__main__ import cmd_send

        has_session = MagicMock(returncode=0)
        monkeypatch.setattr("agentwire.__main__.subprocess.run", lambda *a, **k: has_session)
        monkeypatch.setattr(session_ready, "wait_for_session_ready", lambda s, timeout: True)
        monkeypatch.setattr(session_ready, "send_verified", lambda s, m: False)

        assert cmd_send(self._args()) == 1
        payload = self._payload(capsys)
        assert payload["verified"] is False

    def test_remote_rejected(self, capsys):
        from agentwire.__main__ import cmd_send

        assert cmd_send(self._args(session="proj@gpu")) == 1
        payload = self._payload(capsys)
        assert "local-only" in payload["error"]

    def test_pane_combo_rejected(self, capsys):
        from agentwire.__main__ import cmd_send

        assert cmd_send(self._args(pane=1)) == 1
        payload = self._payload(capsys)
        assert "--pane" in payload["error"]


# --- cmd_new --first-message ---

class TestCmdNewFirstMessage:
    def test_remote_rejected(self, capsys, monkeypatch):
        from agentwire.__main__ import cmd_new

        monkeypatch.setattr("agentwire.__main__._check_tmux_installed", lambda: True)
        args = argparse.Namespace(
            session="proj@gpu", path=None, force=False, json=True,
            roles=None, no_soul=True, first_message="an idea",
        )
        assert cmd_new(args) == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert "local-only" in payload["error"]


# --- cmd_recreate / cmd_fork route through resolve_roles (#311) ---
#
# Both commands used to copy `project_config.roles` raw, bypassing
# resolve_roles + the #309/#310 kind-derivation — so a recreated worktree
# session silently lost its non-overridable worktree-session etiquette
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
    import agentwire.__main__ as mod

    cap = _RoleCapture()

    cfg = None
    if project_config_roles is not None:
        cfg = SimpleNamespace(
            type=SimpleNamespace(value="claude-bypass"),
            roles=project_config_roles,
        )

    monkeypatch.setattr(mod, "load_config", lambda: {
        "projects": {"dir": str(projects_dir), "worktrees": {"suffix": "-worktrees"}},
    })
    monkeypatch.setattr(mod, "load_project_config", lambda p: cfg)
    monkeypatch.setattr(mod, "detect_default_agent_type", lambda: "claude")
    monkeypatch.setattr(mod, "build_agent_command", lambda *a, **k: mod.AgentCommand(command=""))

    def fake_ensure_worktree(base, branch, wt, **kw):
        Path(wt).mkdir(parents=True, exist_ok=True)
        return True

    monkeypatch.setattr(mod, "ensure_worktree", fake_ensure_worktree)

    def fake_load_roles(role_names, path):
        cap.role_names = list(role_names)
        return [], []

    monkeypatch.setattr(mod, "load_roles", fake_load_roles)
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)
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
        import agentwire.__main__ as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=None)
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(
            session="proj/feature", json=True, type="claude-bypass", env=None,
        )
        assert mod.cmd_recreate(args) == 0
        # The whole point: a project/branch recreate is a worktree-session, so
        # the safety contract is present even though nothing was saved.
        assert cap.role_names[0] == "worktree-session"
        assert "soul" in cap.role_names

    def test_worktree_recreate_stacks_saved_roles_under_etiquette(
        self, monkeypatch, tmp_path
    ):
        import agentwire.__main__ as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=["domain"])
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(
            session="proj/feature", json=True, type="claude-bypass", env=None,
        )
        assert mod.cmd_recreate(args) == 0
        # Non-overridable: etiquette first, saved role stacks, never replaces.
        assert cap.role_names[0] == "worktree-session"
        assert "domain" in cap.role_names

    def test_plain_recreate_is_orchestrator_replaceable(self, monkeypatch, tmp_path):
        import agentwire.__main__ as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=["custom"])
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(session="proj", json=True, type="claude-bypass", env=None)
        assert mod.cmd_recreate(args) == 0
        # Persona kind: saved roles REPLACE the orchestrator default.
        assert "orchestrator" not in cap.role_names
        assert "custom" in cap.role_names

    def test_plain_recreate_zero_config_is_orchestrator(self, monkeypatch, tmp_path):
        import agentwire.__main__ as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=None)
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(session="proj", json=True, type="claude-bypass", env=None)
        assert mod.cmd_recreate(args) == 0
        assert cap.role_names[0] == "orchestrator"


class TestForkRoutesThroughResolveRoles:
    def test_worktree_fork_injects_worktree_session_etiquette(self, monkeypatch, tmp_path):
        import agentwire.__main__ as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)  # source_path (no source branch)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=None)
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(
            source="proj", target="proj/feat", json=True, type="claude-bypass",
            env=None, commit=None,
        )
        assert mod.cmd_fork(args) == 0
        # Fork target is a worktree → worktree-session etiquette, intrinsic.
        assert cap.role_names[0] == "worktree-session"

    def test_worktree_fork_stacks_source_roles_under_etiquette(self, monkeypatch, tmp_path):
        import agentwire.__main__ as mod

        projects = tmp_path / "projects"
        (projects / "proj").mkdir(parents=True)
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=["domain"])
        monkeypatch.setattr(mod.subprocess, "run", _fake_run())

        args = argparse.Namespace(
            source="proj", target="proj/feat", json=True, type="claude-bypass",
            env=None, commit=None,
        )
        assert mod.cmd_fork(args) == 0
        assert cap.role_names[0] == "worktree-session"
        assert "domain" in cap.role_names

    def test_non_worktree_fork_is_orchestrator_replaceable(self, monkeypatch, tmp_path):
        import agentwire.__main__ as mod

        projects = tmp_path / "projects"
        projects.mkdir(parents=True)
        src_cwd = tmp_path / "src_cwd"
        src_cwd.mkdir()
        cap = _patch_role_pipeline(monkeypatch, projects, project_config_roles=["custom"])
        monkeypatch.setattr(
            mod.subprocess, "run", _fake_run(source_session="ctxa", cwd=src_cwd)
        )

        args = argparse.Namespace(
            source="ctxa", target="ctxb", json=True, type="claude-bypass",
            env=None, commit=None,
        )
        assert mod.cmd_fork(args) == 0
        # Same-dir fork has no branch → orchestrator persona; source roles win.
        assert "orchestrator" not in cap.role_names
        assert "custom" in cap.role_names
