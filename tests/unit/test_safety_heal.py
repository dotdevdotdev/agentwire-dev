"""Tests for the drift-aware safety heal and doctor's damage-control checks (#462).

`safety install --yes` must run unattended and drift-aware: install missing hook
scripts/rules, refresh *owned* hook scripts that drifted, register missing
matchers — and never clobber an existing (possibly hand-customized) rule.
"""

import json
from pathlib import Path

import pytest

from agentwire import cli_safety


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Redirect every ~/.agentwire and ~/.claude path used by the heal."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cfg = home / ".agentwire"
    monkeypatch.setattr(cli_safety, "CONFIG_DIR", cfg)
    monkeypatch.setattr(cli_safety, "HOOKS_DIR", cfg / "hooks" / "damage-control")
    monkeypatch.setattr(cli_safety, "LOGS_DIR", cfg / "logs" / "damage-control")
    monkeypatch.setattr(cli_safety, "RULES_DIR", cfg / "damage-control")
    monkeypatch.setattr(cli_safety, "TOOLDEFS_DIR", cfg / "tooldefs")
    monkeypatch.setattr(cli_safety, "DAMAGECONTROL_FILE", cfg / "damagecontrol.yml")
    return home


class TestHealIdempotency:
    def test_fresh_heal_installs_everything(self, fake_env):
        summary = cli_safety.heal_damage_control(quiet=True)
        assert summary["hooks_installed"]          # all DC hook scripts
        assert summary["rules_installed"]          # all bundled rules
        assert summary["matchers_added"] == len(cli_safety.DAMAGE_CONTROL_MATCHERS)
        # Every bundled rule landed.
        installed = {p.name for p in cli_safety.RULES_DIR.glob("*.yaml")}
        bundled = {p.name for p in cli_safety.get_damage_control_source().glob("*.yaml")}
        assert bundled <= installed

    def test_second_heal_is_noop(self, fake_env):
        cli_safety.heal_damage_control(quiet=True)
        summary = cli_safety.heal_damage_control(quiet=True)
        assert summary["hooks_installed"] == []
        assert summary["hooks_updated"] == []
        assert summary["rules_installed"] == []
        assert summary["matchers_added"] == 0


class TestHealDriftAwareness:
    def test_missing_rule_reinstalled(self, fake_env):
        cli_safety.heal_damage_control(quiet=True)
        victim = next(cli_safety.RULES_DIR.glob("*.yaml"))
        name = victim.name
        victim.unlink()
        summary = cli_safety.heal_damage_control(quiet=True)
        assert name in summary["rules_installed"]
        assert (cli_safety.RULES_DIR / name).exists()

    def test_customized_rule_survives(self, fake_env):
        cli_safety.heal_damage_control(quiet=True)
        rule = next(cli_safety.RULES_DIR.glob("*.yaml"))
        rule.write_text("# my hand-customized rule\n")
        cli_safety.heal_damage_control(quiet=True)
        # Never blind-clobbered: the customization is intact.
        assert rule.read_text() == "# my hand-customized rule\n"

    def test_stale_owned_hook_refreshed(self, fake_env):
        cli_safety.heal_damage_control(quiet=True)
        hook = cli_safety.HOOKS_DIR / cli_safety.DAMAGE_CONTROL_FILES[0]
        hook.write_text("# stale\n")
        summary = cli_safety.heal_damage_control(quiet=True)
        # Owned hook scripts carry no user edits — drift is overwritten.
        assert hook.name in summary["hooks_updated"]
        assert "# stale" not in hook.read_text()


class TestDriftDetectors:
    def test_hook_drift_states(self, fake_env):
        assert set(cli_safety.damage_control_hook_drift().values()) == {"missing"}
        cli_safety.heal_damage_control(quiet=True)
        assert set(cli_safety.damage_control_hook_drift().values()) == {"ok"}
        hook = cli_safety.HOOKS_DIR / cli_safety.DAMAGE_CONTROL_FILES[0]
        hook.write_text("# drifted\n")
        assert cli_safety.damage_control_hook_drift()[hook.name] == "stale"

    def test_rules_drift_states(self, fake_env):
        assert set(cli_safety.rules_drift().values()) == {"missing"}
        cli_safety.heal_damage_control(quiet=True)
        assert set(cli_safety.rules_drift().values()) == {"ok"}

    def test_missing_matchers(self, fake_env):
        assert set(cli_safety.missing_damage_control_matchers()) == set(
            cli_safety.DAMAGE_CONTROL_MATCHERS
        )
        cli_safety.heal_damage_control(quiet=True)
        assert cli_safety.missing_damage_control_matchers() == []


class TestInstallCmdNonInteractive:
    def test_yes_does_not_prompt(self, fake_env, monkeypatch):
        # input() must never be called in --yes mode.
        monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted"))
        rc = cli_safety.safety_install_cmd(assume_yes=True)
        assert rc == 0
        assert (cli_safety.HOOKS_DIR / cli_safety.DAMAGE_CONTROL_FILES[0]).exists()


class TestDoctorDamageControlSection:
    @pytest.fixture(autouse=True)
    def _healed(self, fake_env):
        cli_safety.heal_damage_control(quiet=True)
        return fake_env

    def _patch_safety_enabled(self, monkeypatch, enabled):
        # The kill switch now lives in the host-owned damagecontrol.yml, read by
        # load_safety_config (#466). Write it directly in the fake home.
        cli_safety.DAMAGECONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
        cli_safety.DAMAGECONTROL_FILE.write_text(
            f"enabled: {str(bool(enabled)).lower()}\n"
        )

    def test_clean_when_healed_and_enabled(self, monkeypatch, capsys):
        from agentwire.__main__ import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, True)
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues == 0
        assert "[ok] Damage control enabled" in out

    def test_disabled_kill_switch_flagged(self, monkeypatch, capsys):
        from agentwire.__main__ import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, False)
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "DISABLED" in out

    def test_missing_rule_flagged(self, monkeypatch, capsys):
        from agentwire.__main__ import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, True)
        next(cli_safety.RULES_DIR.glob("*.yaml")).unlink()
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "rules NOT installed" in out

    def test_missing_matcher_flagged(self, monkeypatch, capsys):
        from agentwire.__main__ import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, True)
        settings = Path.home() / ".claude" / "settings.json"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "matchers not registered" in out
