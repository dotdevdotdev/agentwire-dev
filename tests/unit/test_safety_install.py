"""AgentWire's install of the damage-control package, and its doctor section (#462, #1209 M6).

``safety install`` / ``hooks install`` are ``safety_ext.install``: provenance for BOTH packages,
the profile manifest, then the package's own ``init`` (self-test before anything switches, one
atomic settings write, an install record). Pre-package leftovers are ``safety migrate``'s job
(``test_safety_migrate.py``).
"""

import json
import sys
from pathlib import Path

import pytest
from damage_control import engine, install
from damage_control import claude_code as cc
from damage_control.profile import GENERIC

from agentwire import safety_commands, safety_ext
from agentwire.safety import provenance as prov


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """A temp HOME with AgentWire's profile active; no real self-test, a known Claude Code."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTWIRE_DIR", str(home / ".agentwire"))
    # Machine-global installs are refused from a non-canonical package (#936). Pin both running
    # packages AS canonical so these measure the install, not the guard.
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.setattr(prov, "canonical_package_dir", lambda package="agentwire": prov.running_package_dir(package))
    monkeypatch.setattr(install, "install_source", lambda: "pypi")
    cfg = home / ".agentwire"
    monkeypatch.setattr(safety_commands, "CONFIG_DIR", cfg)
    monkeypatch.setattr(safety_commands, "LOGS_DIR", cfg / "logs" / "damage-control")
    monkeypatch.setattr(safety_commands, "RULES_DIR", cfg / "damage-control")
    monkeypatch.setattr(safety_commands, "TOOLDEFS_DIR", cfg / "tooldefs")
    monkeypatch.setattr(safety_commands, "DAMAGECONTROL_FILE", cfg / "damagecontrol.yml")
    yield home
    engine.install_profile(GENERIC)
    safety_ext.activate()


def _registered(home):
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    return settings["hooks"]["PreToolUse"]


class TestInstall:
    def test_fresh_install_registers_the_package_and_second_install_changes_nothing(self, fake_env):
        first = safety_ext.install(log=lambda *_: None)
        assert (first.code, first.changed, first.refused) == (0, True, False)
        assert safety_ext.manifest_path().is_file()
        assert install.record_path().is_file()
        assert install.breakglass_path().is_file()
        # The rules are the package's: nothing is copied into the host's overlay dirs.
        assert not safety_commands.RULES_DIR.exists() or not list(safety_commands.RULES_DIR.iterdir())

        second = safety_ext.install(log=lambda *_: None)
        assert (second.code, second.changed) == (0, False)

    def test_registration_is_exec_form_with_the_recorded_interpreter_and_fails_closed(self, fake_env):
        safety_ext.install(log=lambda *_: None)
        bad = []
        # Every matcher the package defines, PowerShell included, is registered (#1218).
        assert {e["matcher"] for e in _registered(fake_env)} == set(cc.MATCHERS)
        for entry in _registered(fake_env):
            (hook,) = entry["hooks"]
            kind = cc.MATCHERS[entry["matcher"]]
            want = {"type": "command", "command": sys.executable, "onFailure": "block",
                    "timeout": engine.HOOK_TIMEOUT_SECONDS}
            if {k: hook.get(k) for k in want} != want or hook["args"][:4] != ["-I", "-m", "damage_control.hook", kind]:
                bad.append((entry["matcher"], hook))
            elif hook["args"][4:6] != ["--profile", str(safety_ext.manifest_path())]:
                bad.append((entry["matcher"], "profile", hook["args"]))
        assert not bad, bad

    def test_a_failed_selftest_leaves_the_existing_registration_alone(self, fake_env, monkeypatch):
        existing = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/other-hook"}]}
        settings = fake_env / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [existing]}}))
        monkeypatch.setattr(install, "selftest", lambda interpreter, profile=None: ["bash: exit 1, expected 2"])
        lines = []

        outcome = safety_ext.install(log=lines.append)

        assert outcome.code == 1 and not outcome.changed
        assert any("self-test failed" in line for line in lines), lines
        assert json.loads(settings.read_text())["hooks"]["PreToolUse"] == [existing]
        assert not install.record_path().exists()

    def test_a_foreign_package_writes_nothing_and_names_which_one(self, fake_env, monkeypatch):
        # Only the ENGINE is foreign: guarding `agentwire` alone would leave the package that
        # runs on every tool call unguarded (#1209 M6, the doubled #936 hazard).
        real = prov.canonical_package_dir
        monkeypatch.setattr(prov, "canonical_package_dir",
                            lambda package="agentwire": Path("/nonexistent/site-packages/damage_control")
                            if package == "damage_control" else real(package))
        lines = []

        outcome = safety_ext.install(log=lines.append)

        assert (outcome.code, outcome.refused, outcome.changed) == (1, True, False)
        assert any("damage_control" in line for line in lines), lines
        assert not safety_ext.manifest_path().exists()
        assert not (fake_env / ".claude" / "settings.json").exists()

    def test_status_reports_the_package_version_and_profile(self, fake_env):
        safety_ext.install(log=lambda *_: None)
        report = safety_ext.status()
        assert report["profile"] == "agentwire" and report["version"] and report["ok"], report


class TestDoctorDamageControlSection:
    @pytest.fixture(autouse=True)
    def _installed(self, fake_env):
        safety_ext.install(log=lambda *_: None)
        return fake_env

    def _write_policy(self, enabled):
        # The kill switch lives in the host-owned damagecontrol.yml, read by load_safety_config (#466).
        safety_commands.DAMAGECONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
        safety_commands.DAMAGECONTROL_FILE.write_text(f"enabled: {str(bool(enabled)).lower()}\npacks: []\n")

    def test_disabled_kill_switch_flagged(self, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._write_policy(False)
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "DISABLED" in out

    def test_doctor_names_the_package_version_and_profile(self, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._write_policy(True)
        _render_damage_control_section()
        out = capsys.readouterr().out
        assert "damage-control-dev " in out and "profile 'agentwire'" in out, out

    def test_missing_matcher_flagged(self, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._write_policy(True)
        (Path.home() / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}))
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "[!!]" in out

    def test_a_dead_recorded_interpreter_is_flagged_and_named(self, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._write_policy(True)
        settings = Path.home() / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        for entry in data["hooks"]["PreToolUse"]:
            for hook in entry["hooks"]:
                hook["command"] = "/nonexistent/venv/bin/python"
        settings.write_text(json.dumps(data))
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1 and "/nonexistent/venv/bin/python" in out

    def test_a_legacy_script_registration_is_flagged(self, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._write_policy(True)
        settings = Path.home() / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        data["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [
            {"type": "command", "command": "~/.agentwire/hooks/damage-control/bash-tool-damage-control.py"}]})
        settings.write_text(json.dumps(data))
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1 and "pre-package" in out.lower()


class TestStatusReportsWiring:
    """#1085: a rule set on disk but nothing registered must not read as healthy."""

    def test_status_flags_unregistered_hooks_and_clears_after_install(self, fake_env):
        safety_ext.install(log=lambda *_: None)
        (Path.home() / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))

        out = safety_commands.format_safety_status(safety_commands.get_safety_status())
        assert "may NOT be running" in out

        safety_ext.install(log=lambda *_: None)
        out = safety_commands.format_safety_status(safety_commands.get_safety_status())
        assert "may NOT be running" not in out
