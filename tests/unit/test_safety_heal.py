"""Tests for the heal and doctor's damage-control checks (#462, reshaped by #1209 M4).

``safety install --yes`` / ``hooks install`` run unattended: write the profile manifest and the
break-glass script, retire stale rule copies older versions left behind, PROVE the package hook
as registered, and only then switch the registration to exec form in one atomic settings write.
The hooks themselves are not installed any more -- the package ships them.
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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTWIRE_DIR", str(home / ".agentwire"))
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
    monkeypatch.setattr(safety_commands, "LEGACY_HOOKS_DIR", cfg / "hooks" / "damage-control")
    monkeypatch.setattr(safety_commands, "LOGS_DIR", cfg / "logs" / "damage-control")
    monkeypatch.setattr(safety_commands, "RULES_DIR", cfg / "damage-control")
    monkeypatch.setattr(safety_commands, "TOOLDEFS_DIR", cfg / "tooldefs")
    monkeypatch.setattr(safety_commands, "DAMAGECONTROL_FILE", cfg / "damagecontrol.yml")
    monkeypatch.setattr(safety_commands, "INSTALL_RECORD", cfg / "damagecontrol-install.json")
    monkeypatch.setattr(safety_commands, "BREAKGLASS_DIR", cfg / "bin")
    return home


def _registered(home):
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    return settings["hooks"]["PreToolUse"]


class TestHealIdempotency:
    def test_fresh_heal_registers_the_package_and_second_heal_is_a_noop(self, fake_env):
        first = safety_commands.heal_damage_control(quiet=True)
        assert first["matchers_added"] == len(safety_commands.DAMAGE_CONTROL_MATCHERS)
        assert Path(first["profile_written"]).is_file()
        assert Path(first["breakglass"]).is_file()
        assert safety_commands.INSTALL_RECORD.is_file()
        # The rules are the package's: nothing is copied into the host's overlay dirs.
        assert not safety_commands.RULES_DIR.exists() or not list(safety_commands.RULES_DIR.iterdir())
        assert not safety_commands.TOOLDEFS_DIR.exists() or not list(safety_commands.TOOLDEFS_DIR.iterdir())

        second = safety_commands.heal_damage_control(quiet=True)
        assert second["matchers_added"] == 0

    def test_registration_is_exec_form_with_the_recorded_interpreter_and_fails_closed(self, fake_env):
        import sys

        safety_commands.heal_damage_control(quiet=True)
        bad = []
        for entry in _registered(fake_env):
            (hook,) = entry["hooks"]
            kind = safety_commands.DAMAGE_CONTROL_MATCHERS[entry["matcher"]]
            want = {"type": "command", "command": sys.executable, "onFailure": "block",
                    "timeout": safety_commands.HOOK_TIMEOUT_SECONDS}
            if {k: hook.get(k) for k in want} != want or hook["args"][:4] != ["-I", "-m", "damage_control.hook", kind]:
                bad.append((entry["matcher"], hook))
            elif hook["args"][4:6] != ["--profile", str(safety_commands._hook_args(kind)[5])]:
                bad.append((entry["matcher"], "profile", hook["args"]))
        assert not bad, bad

    def test_a_failed_selftest_leaves_the_existing_registration_alone(self, fake_env, monkeypatch):
        legacy = {"matcher": "Bash", "hooks": [{"type": "command",
                  "command": "~/.agentwire/hooks/damage-control/bash-tool-damage-control.py"}]}
        settings = fake_env / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [legacy]}}))
        monkeypatch.setattr(safety_commands, "damage_control_selftest", lambda interpreter=None: ["bash: exit 1, expected 2"])

        summary = safety_commands.heal_damage_control(quiet=True)

        assert summary["selftest_failures"] == ["bash: exit 1, expected 2"]
        assert json.loads(settings.read_text())["hooks"]["PreToolUse"] == [legacy]
        assert not safety_commands.INSTALL_RECORD.exists()

    def test_legacy_script_registrations_are_replaced_in_one_write_and_the_files_removed(self, fake_env):
        legacy_dir = safety_commands.LEGACY_HOOKS_DIR
        legacy_dir.mkdir(parents=True)
        for name in safety_commands.LEGACY_HOOK_FILES:
            (legacy_dir / name).write_text("#!/usr/bin/env python3\n")
        settings = fake_env / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/my-own-hook"}]}
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": str(legacy_dir / "bash-tool-damage-control.py")}]},
            mine,
        ]}}))

        summary = safety_commands.heal_damage_control(quiet=True)

        assert sorted(summary["legacy_hooks_removed"]) == sorted(safety_commands.LEGACY_HOOK_FILES)
        assert not legacy_dir.exists()
        pre = _registered(fake_env)
        assert safety_commands.legacy_registrations() == []
        assert mine in pre, "a hook that is not ours must survive the replacement"
        assert safety_commands.missing_damage_control_matchers() == []

    def test_a_stale_pristine_overlay_copy_is_retired_and_an_edited_one_is_kept(self, fake_env, monkeypatch):
        baselines = safety_commands.load_rule_baselines()
        (name, hashes), = [(n, h) for n, h in sorted(baselines["rules"].items())][:1]
        assert hashes, "baselines carry at least one shipped hash"
        pristine = safety_commands.RULES_DIR / name
        pristine.parent.mkdir(parents=True)
        # Stand-in for a copy whose bytes hash to a shipped version.
        edited = safety_commands.RULES_DIR / "mine-edited.yaml"
        edited.write_text("bashToolPatterns: []\n")
        pristine.write_text("# stand-in\n")
        real_sha = safety_commands._sha256
        monkeypatch.setattr(safety_commands, "_sha256", lambda p: hashes[0] if p == pristine else real_sha(p))
        summary = safety_commands.heal_damage_control(quiet=True)
        assert name in summary["rules_retired"] and not pristine.exists()
        assert edited.exists()


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

    def test_a_dead_recorded_interpreter_is_flagged_and_named(self, monkeypatch, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, True)
        record = json.loads(safety_commands.INSTALL_RECORD.read_text())
        record["interpreter"] = "/nonexistent/venv/bin/python"
        safety_commands.INSTALL_RECORD.write_text(json.dumps(record))
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1 and "/nonexistent/venv/bin/python" in out

    def test_a_legacy_script_registration_is_flagged(self, monkeypatch, capsys):
        from agentwire.doctor_cli import _render_damage_control_section
        self._patch_safety_enabled(monkeypatch, True)
        settings = Path.home() / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        data["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [
            {"type": "command", "command": "~/.agentwire/hooks/damage-control/bash-tool-damage-control.py"}]})
        settings.write_text(json.dumps(data))
        issues = _render_damage_control_section()
        out = capsys.readouterr().out
        assert issues >= 1 and "legacy" in out.lower()


class TestStatusReportsWiring:
    """#1085: a rule set on disk but nothing registered must not read as healthy."""

    def test_status_flags_unregistered_hooks_and_clears_after_heal(self, fake_env):
        safety_commands.heal_damage_control(quiet=True)
        settings = Path.home() / ".claude" / "settings.json"
        settings.write_text(json.dumps({"hooks": {}}))

        status = safety_commands.get_safety_status()
        assert set(status["missing_matchers"]) == set(safety_commands.DAMAGE_CONTROL_MATCHERS)
        out = safety_commands.format_safety_status(status)
        assert "NOT WIRED UP" in out and "✓ PreToolUse matchers" not in out

        safety_commands.heal_damage_control(quiet=True)
        status = safety_commands.get_safety_status()
        assert status["missing_matchers"] == []
        assert "NOT WIRED UP" not in safety_commands.format_safety_status(status)
