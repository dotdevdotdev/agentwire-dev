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
    # Machine-global installs are refused from a non-canonical package (#936).
    # Pin the running package AS canonical so these measure the install, not
    # the guard, and behave the same in a worktree as in CI's plain clone.
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    from agentwire.safety import provenance as _prov
    monkeypatch.setattr(
        _prov, "canonical_package_dir",
        lambda: Path(__import__("agentwire").__file__).parent.resolve(),
    )

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
        # Every bundled rule of a default group landed (packs are opt-in, #1141).
        from agentwire.safety.groups import REGISTRY
        installed = {p.name for p in safety_commands.RULES_DIR.glob("*.yaml")}
        bundled = {p.name for p in safety_commands.get_damage_control_source().glob("*.yaml")}
        pack_rules = {f for e in REGISTRY.values() if e.kind == "pack" for f in e.rules}
        assert bundled - pack_rules <= installed
        assert not pack_rules & installed


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
            f"enabled: {str(bool(enabled)).lower()}\npacks: []\n"
        )


    def test_disabled_kill_switch_flagged(self, monkeypatch, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, False)
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "DISABLED" in out


    def test_missing_matcher_flagged(self, monkeypatch, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, True)
        settings = Path.home() / ".claude" / "settings.json"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1
        assert "matchers not registered" in out


class TestStatusReportsWiring:
    """#1085: scripts on disk but nothing registered must not read as healthy."""

    def test_status_flags_unregistered_hooks_and_clears_after_heal(self, fake_env):
        safety_commands.heal_damage_control(quiet=True)
        settings = Path.home() / ".claude" / "settings.json"
        settings.write_text(json.dumps({"hooks": {}}))

        status = safety_commands.get_safety_status()
        assert status["hooks_installed"]  # the old, misleading signal
        assert set(status["missing_matchers"]) == set(safety_commands.DAMAGE_CONTROL_MATCHERS)
        out = safety_commands.format_safety_status(status)
        assert "NOT WIRED UP" in out and "✓ PreToolUse matchers" not in out

        safety_commands.heal_damage_control(quiet=True)
        status = safety_commands.get_safety_status()
        assert status["missing_matchers"] == []
        assert "NOT WIRED UP" not in safety_commands.format_safety_status(status)
