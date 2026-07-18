"""Tests for the out-of-tree project registry and `agentwire projects add` (#814)."""

import argparse
import json
import shlex

import pytest

# --- registry module: load/add/remove/is_registered ---

class TestRegistry:
    @pytest.fixture(autouse=True)
    def _isolate_registry(self, tmp_path, monkeypatch):
        import agentwire.projects as mod
        monkeypatch.setattr(mod, "PROJECTS_REGISTRY_FILE", tmp_path / "projects.json")
        self.mod = mod

    def test_load_missing_file_returns_empty(self):
        assert self.mod.load_registry() == []

    def test_add_then_load(self):
        assert self.mod.add_registry_entry("/tmp/foo", "local") is True
        assert self.mod.load_registry() == [{"path": "/tmp/foo", "machine": "local"}]

    def test_add_is_idempotent(self):
        self.mod.add_registry_entry("/tmp/foo", "local")
        assert self.mod.add_registry_entry("/tmp/foo", "local") is False
        assert len(self.mod.load_registry()) == 1

    def test_add_same_path_different_machine(self):
        self.mod.add_registry_entry("/tmp/foo", "local")
        assert self.mod.add_registry_entry("/tmp/foo", "remote-1") is True
        assert len(self.mod.load_registry()) == 2

    def test_is_registered(self):
        assert self.mod.is_registered("/tmp/foo") is False
        self.mod.add_registry_entry("/tmp/foo", "local")
        assert self.mod.is_registered("/tmp/foo") is True
        assert self.mod.is_registered("/tmp/foo", "remote-1") is False

    def test_remove_entry(self):
        self.mod.add_registry_entry("/tmp/foo", "local")
        assert self.mod.remove_registry_entry("/tmp/foo", "local") is True
        assert self.mod.load_registry() == []

    def test_remove_missing_entry_is_noop(self):
        assert self.mod.remove_registry_entry("/tmp/nope", "local") is False

    def test_malformed_json_treated_as_empty(self):
        self.mod.PROJECTS_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.mod.PROJECTS_REGISTRY_FILE.write_text("not json")
        assert self.mod.load_registry() == []


# --- get_projects(): registry entries surface alongside the projects.dir scan ---

class TestGetProjectsWithRegistry:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import agentwire.projects as mod
        self.mod = mod
        self.tmp_path = tmp_path
        monkeypatch.setattr(mod, "PROJECTS_REGISTRY_FILE", tmp_path / "projects.json")
        monkeypatch.setattr(mod, "_get_all_machines", lambda: [])

        from types import SimpleNamespace
        scanned_dir = tmp_path / "projects"
        scanned_dir.mkdir()
        fake_config = SimpleNamespace(projects=SimpleNamespace(dir=scanned_dir))
        monkeypatch.setattr(mod, "get_config", lambda: fake_config)

    def test_registry_project_appears_alongside_scanned(self):
        scanned = self.tmp_path / "projects" / "in-tree"
        scanned.mkdir()
        (scanned / ".agentwire.yml").write_text("posture: bypass\n")

        out_of_tree = self.tmp_path / "elsewhere"
        out_of_tree.mkdir()
        (out_of_tree / ".agentwire.yml").write_text("posture: bypass\n")
        self.mod.add_registry_entry(str(out_of_tree), "local")

        names = {p["name"] for p in self.mod.get_projects()}
        assert names == {"in-tree", "elsewhere"}

    def test_registry_entry_does_not_shadow_scanned(self):
        """A registry entry that duplicates a scanned path doesn't double-list it."""
        scanned = self.tmp_path / "projects" / "dup"
        scanned.mkdir()
        (scanned / ".agentwire.yml").write_text("posture: bypass\n")
        self.mod.add_registry_entry(str(scanned), "local")

        matches = [p for p in self.mod.get_projects() if p["name"] == "dup"]
        assert len(matches) == 1


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

        monkeypatch.setattr(mod, "_get_machine_config", lambda mid: {"id": mid, "host": "example.com"})

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

    def test_quoted_path_used_for_both_test_and_cat(self, monkeypatch):
        import agentwire.projects as mod

        monkeypatch.setattr(mod, "_get_machine_config", lambda mid: {"id": mid, "host": "example.com"})

        captured = {}

        def fake_run_ssh_command(machine, command, timeout=10):
            captured["cmd"] = command
            return False, ""

        monkeypatch.setattr(mod, "_run_ssh_command", fake_run_ssh_command)

        malicious = "/tmp/$(whoami)"
        mod._resolve_extra_projects([{"path": malicious, "machine": "remote-1"}])

        quoted = shlex.quote(malicious)
        cmd = captured["cmd"]
        assert cmd.count(quoted) == 4  # the -d test, the -f test, and both cat invocations


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

    def test_binds_fresh_out_of_tree_folder(self, tmp_path, capsys):
        target = tmp_path / "somewhere" / "myproj"
        target.mkdir(parents=True)

        rc, payload = self._run(capsys, _add_args(str(target)))

        assert rc == 0
        assert payload["success"] is True
        assert payload["already_bound"] is False
        assert payload["wrote_config"] is True
        assert payload["mechanism"] == "registry"
        assert (target / ".agentwire.yml").exists()
        assert self.projects_mod.is_registered(str(target), "local")

    def test_child_of_projects_dir_skips_registry(self, capsys):
        target = self.projects_dir / "child-proj"
        target.mkdir()

        rc, payload = self._run(capsys, _add_args(str(target)))

        assert rc == 0
        assert payload["mechanism"] == "scan"
        assert not self.projects_mod.is_registered(str(target), "local")

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

    def test_nonexistent_path_fails(self, tmp_path, capsys):
        rc, payload = self._run(capsys, _add_args(str(tmp_path / "does-not-exist")))
        assert rc == 1
        assert payload["success"] is False

    def test_file_not_directory_fails(self, tmp_path, capsys):
        f = tmp_path / "afile.txt"
        f.write_text("hi")
        rc, payload = self._run(capsys, _add_args(str(f)))
        assert rc == 1
        assert payload["success"] is False

    def test_symlink_resolves_to_canonical_target(self, tmp_path, capsys):
        real = tmp_path / "real-proj"
        real.mkdir()
        link = tmp_path / "link-proj"
        link.symlink_to(real)

        rc, payload = self._run(capsys, _add_args(str(link)))

        assert rc == 0
        assert payload["path"] == str(real)

    def test_check_mode_does_not_write_or_register(self, tmp_path, capsys):
        target = tmp_path / "dry-run-proj"
        target.mkdir()

        rc, payload = self._run(capsys, _add_args(str(target), check=True))

        assert rc == 0
        assert payload["dry_run"] is True
        assert payload["wrote_config"] is False
        assert not (target / ".agentwire.yml").exists()
        assert not self.projects_mod.is_registered(str(target), "local")

    def test_check_mode_then_real_bind_writes(self, tmp_path, capsys):
        target = tmp_path / "two-step-proj"
        target.mkdir()

        self._run(capsys, _add_args(str(target), check=True))
        rc, payload = self._run(capsys, _add_args(str(target), check=False))

        assert rc == 0
        assert payload["wrote_config"] is True
        assert (target / ".agentwire.yml").exists()

    def test_unknown_machine_fails(self, tmp_path, capsys, monkeypatch):
        import agentwire.core as core_mod
        monkeypatch.setattr(core_mod, "_get_machine_config", lambda mid: None)

        rc, payload = self._run(capsys, _add_args("/tmp/whatever", machine="ghost"))
        assert rc == 1
        assert payload["success"] is False
        assert "ghost" in payload["error"]

    def test_empty_path_fails(self, capsys):
        rc, payload = self._run(capsys, _add_args(""))
        assert rc == 1
        assert payload["success"] is False
