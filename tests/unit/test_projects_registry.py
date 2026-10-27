"""Tests for the out-of-tree project registry and `agentwire projects add` (#814)."""

import argparse
import json
import shlex

import pytest

# --- registry module: load/add/remove/is_registered ---


# --- get_projects(): registry entries surface alongside the projects.dir scan ---


# --- _resolve_extra_projects: remote path must never reach the shell unquoted ---
#
# `path` here is registry-supplied (ultimately user input via `agentwire
# projects add` / POST /api/projects/bind), and _resolve_extra_projects
# splices it into a command string handed to a remote shell over SSH on
# every get_projects() poll. A shell metacharacter in a stored path is a
# command-injection vector against the remote machine unless it's quoted.


class TestResolveExtraProjectsRemoteShellSafety:
    def test_malicious_path_is_quoted_as_a_single_token(self, monkeypatch):
        import agentwire.projects as mod

        monkeypatch.setattr(
            mod, "_get_machine_config", lambda mid: {"id": mid, "host": "example.com"}
        )

        captured = {}

        def fake_run_ssh_command(machine, command, timeout=10):
            captured["cmd"] = command
            return False, ""

        monkeypatch.setattr(mod, "_run_ssh_command", fake_run_ssh_command)

        malicious = '/tmp/x"; touch /tmp/pwned; echo "'
        mod._resolve_extra_projects([{"path": malicious, "machine": "remote-1"}])

        cmd = captured["cmd"]
        # The generated `[ -d ... ]` line must be shell-quoted, so a POSIX
        # shell tokenizes the malicious string as ONE literal argument —
        # never as separate injected commands.
        d_line = next(line for line in cmd.splitlines() if line.strip().startswith("if [ -d"))
        assert malicious in shlex.split(d_line)


# --- cmd_projects_add: the CLI command itself ---


def _add_args(path, machine=None, check=False, json_mode=True):
    return argparse.Namespace(path=path, machine=machine, check=check, json=json_mode)


class TestCmdProjectsAdd:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import agentwire.config as config_mod
        import agentwire.projects as projects_mod

        self.tmp_path = tmp_path
        self.projects_mod = projects_mod
        monkeypatch.setattr(projects_mod, "PROJECTS_REGISTRY_FILE", tmp_path / "registry.json")

        from types import SimpleNamespace

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        self.projects_dir = projects_dir
        fake_config = SimpleNamespace(projects=SimpleNamespace(dir=projects_dir))
        monkeypatch.setattr(config_mod, "get_config", lambda: fake_config)

    def _run(self, capsys, args):
        from agentwire.roles_cli import cmd_projects_add

        rc = cmd_projects_add(args)
        out = capsys.readouterr().out
        return rc, (json.loads(out) if out.strip() else {})

    def test_collision_reports_already_bound_without_overwriting(self, tmp_path, capsys):
        target = tmp_path / "existing"
        target.mkdir()
        config_file = target / ".agentwire.yml"
        config_file.write_text("posture: auto\nroles: [custom]\n")

        rc, payload = self._run(capsys, _add_args(str(target)))

        assert rc == 0
        assert payload["already_bound"] is True
        assert payload["wrote_config"] is False
        # Existing config is untouched, not overwritten with defaults.
        assert "posture: auto" in config_file.read_text()
