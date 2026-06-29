"""Tests for the drift-aware safety heal and doctor's damage-control checks (#462).

`safety install --yes` must run unattended and drift-aware: install missing hook
scripts/rules, refresh *owned* hook scripts that drifted, register missing
matchers — and never clobber an existing (possibly hand-customized) rule.
"""

import json
from pathlib import Path

import pytest

from agentwire import safety_commands


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Redirect every ~/.agentwire and ~/.claude path used by the heal."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cfg = home / ".agentwire"
    monkeypatch.setattr(safety_commands, "CONFIG_DIR", cfg)
    monkeypatch.setattr(safety_commands, "HOOKS_DIR", cfg / "hooks" / "damage-control")
    monkeypatch.setattr(safety_commands, "LOGS_DIR", cfg / "logs" / "damage-control")
    monkeypatch.setattr(safety_commands, "RULES_DIR", cfg / "damage-control")
    monkeypatch.setattr(safety_commands, "TOOLDEFS_DIR", cfg / "tooldefs")
    monkeypatch.setattr(safety_commands, "DAMAGECONTROL_FILE", cfg / "damagecontrol.yml")
    return home


class TestHealIdempotency:
    def test_fresh_heal_installs_everything(self, fake_env):
        summary = safety_commands.heal_damage_control(quiet=True)
        assert summary["hooks_installed"]          # all DC hook scripts
        assert summary["rules_installed"]          # all bundled rules
        assert summary["matchers_added"] == len(safety_commands.DAMAGE_CONTROL_MATCHERS)
        # Every bundled rule landed.
        installed = {p.name for p in safety_commands.RULES_DIR.glob("*.yaml")}
        bundled = {p.name for p in safety_commands.get_damage_control_source().glob("*.yaml")}
        assert bundled <= installed

    def test_second_heal_is_noop(self, fake_env):
        safety_commands.heal_damage_control(quiet=True)
        summary = safety_commands.heal_damage_control(quiet=True)
        assert summary["hooks_installed"] == []
        assert summary["hooks_updated"] == []
        assert summary["rules_installed"] == []
        assert summary["matchers_added"] == 0


class TestHealDriftAwareness:
    def test_missing_rule_reinstalled(self, fake_env):
        safety_commands.heal_damage_control(quiet=True)
        victim = next(safety_commands.RULES_DIR.glob("*.yaml"))
        name = victim.name
        victim.unlink()
        summary = safety_commands.heal_damage_control(quiet=True)
        assert name in summary["rules_installed"]
        assert (safety_commands.RULES_DIR / name).exists()

    def test_customized_rule_survives(self, fake_env):
        safety_commands.heal_damage_control(quiet=True)
        rule = next(safety_commands.RULES_DIR.glob("*.yaml"))
        rule.write_text("# my hand-customized rule\n")
        safety_commands.heal_damage_control(quiet=True)
        # Never blind-clobbered: the customization is intact.
        assert rule.read_text() == "# my hand-customized rule\n"

    def test_stale_owned_hook_refreshed(self, fake_env):
        safety_commands.heal_damage_control(quiet=True)
        hook = safety_commands.HOOKS_DIR / safety_commands.DAMAGE_CONTROL_FILES[0]
        hook.write_text("# stale\n")
        summary = safety_commands.heal_damage_control(quiet=True)
        # Owned hook scripts carry no user edits — drift is overwritten.
        assert hook.name in summary["hooks_updated"]
        assert "# stale" not in hook.read_text()


class TestDriftDetectors:
    def test_hook_drift_states(self, fake_env):
        assert set(safety_commands.damage_control_hook_drift().values()) == {"missing"}
        safety_commands.heal_damage_control(quiet=True)
        assert set(safety_commands.damage_control_hook_drift().values()) == {"ok"}
        hook = safety_commands.HOOKS_DIR / safety_commands.DAMAGE_CONTROL_FILES[0]
        hook.write_text("# drifted\n")
        assert safety_commands.damage_control_hook_drift()[hook.name] == "stale"

    def test_rules_drift_states(self, fake_env):
        assert set(safety_commands.rules_drift().values()) == {"missing"}
        safety_commands.heal_damage_control(quiet=True)
        assert set(safety_commands.rules_drift().values()) == {"ok"}

    def test_missing_matchers(self, fake_env):
        assert set(safety_commands.missing_damage_control_matchers()) == set(
            safety_commands.DAMAGE_CONTROL_MATCHERS
        )
        safety_commands.heal_damage_control(quiet=True)
        assert safety_commands.missing_damage_control_matchers() == []

    def test_partial_shared_command_matcher_still_flagged(self, fake_env):
        """Read/Grep/Glob share one hook script. Registering only Read must NOT
        mark Grep/Glob as present — a command-only check would (the bug); the
        (matcher, command) check catches the gap."""
        settings = Path.home() / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        cmd = "~/.agentwire/hooks/damage-control/read-tool-damage-control.py"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Read", "hooks": [{"type": "command", "command": cmd}]},
        ]}}))
        missing = set(safety_commands.missing_damage_control_matchers())
        assert "Read" not in missing            # the one we registered
        assert {"Grep", "Glob"} <= missing      # genuinely absent, must be flagged


class TestInstallCmdNonInteractive:
    def test_yes_does_not_prompt(self, fake_env, monkeypatch):
        # input() must never be called in --yes mode.
        monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted"))
        rc = safety_commands.safety_install_cmd(assume_yes=True)
        assert rc == 0
        assert (safety_commands.HOOKS_DIR / safety_commands.DAMAGE_CONTROL_FILES[0]).exists()


class TestDoctorDamageControlSection:
    @pytest.fixture(autouse=True)
    def _healed(self, fake_env):
        safety_commands.heal_damage_control(quiet=True)
        return fake_env

    def _patch_safety_enabled(self, monkeypatch, enabled):
        # The kill switch now lives in the host-owned damagecontrol.yml, read by
        # load_safety_config (#466). Write it directly in the fake home.
        safety_commands.DAMAGECONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
        safety_commands.DAMAGECONTROL_FILE.write_text(
            f"enabled: {str(bool(enabled)).lower()}\n"
        )

    def test_clean_when_healed_and_enabled(self, monkeypatch, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, True)
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues == 0
        assert "[ok] Damage control enabled" in out

    def test_disabled_kill_switch_flagged(self, monkeypatch, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, False)
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "DISABLED" in out

    def test_missing_rule_flagged(self, monkeypatch, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, True)
        next(safety_commands.RULES_DIR.glob("*.yaml")).unlink()
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "rules NOT installed" in out

    def test_missing_matcher_flagged(self, monkeypatch, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, True)
        settings = Path.home() / ".claude" / "settings.json"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "matchers not registered" in out
