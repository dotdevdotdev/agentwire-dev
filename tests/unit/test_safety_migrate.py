"""``agentwire safety migrate``: a pre-package install is brought onto the package, safely (#1209 M6)."""

import json
from types import SimpleNamespace

from damage_control import install

from agentwire import safety_commands, safety_migrate
from tests.unit.test_safety_install import fake_env  # noqa: F401  (fixture)

LEGACY_SCRIPT = "bash-tool-damage-control.py"
MINE = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/my-own-hook"}]}


def _legacy_machine(home):
    legacy_dir = home / ".agentwire" / "hooks" / "damage-control"
    legacy_dir.mkdir(parents=True)
    for name in safety_migrate.LEGACY_HOOK_FILES:
        (legacy_dir / name).write_text("#!/usr/bin/env python3\n")
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": str(legacy_dir / LEGACY_SCRIPT)}]},
        MINE,
    ]}}))
    return legacy_dir, settings


def _commands(settings):
    pre = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    return [h.get("command", "") for e in pre for h in e["hooks"]]


def test_legacy_registrations_are_replaced_the_files_removed_and_foreign_hooks_kept(fake_env):  # noqa: F811
    legacy_dir, settings = _legacy_machine(fake_env)

    assert safety_migrate.migrate(log=lambda *_: None) == 0

    assert not legacy_dir.exists()
    assert not [c for c in _commands(settings) if c.endswith(safety_migrate.LEGACY_HOOK_SUFFIX)]
    assert "/usr/local/bin/my-own-hook" in _commands(settings), "a hook that is not ours must survive"
    assert install.record_path().is_file(), "the package hooks are registered and recorded"
    assert safety_migrate.legacy_registrations(safety_migrate.read_user_settings()) == []


def test_a_failed_package_install_leaves_the_old_registrations_and_files(fake_env, monkeypatch):  # noqa: F811
    """Remove the old only once the new is proven: a self-test failure must not strip protection."""
    legacy_dir, settings = _legacy_machine(fake_env)
    before = settings.read_bytes()
    monkeypatch.setattr(install, "selftest", lambda interpreter, profile=None: ["bash: exit 1, expected 2"])

    assert safety_migrate.migrate(log=lambda *_: None) == 1

    assert settings.read_bytes() == before
    assert (legacy_dir / LEGACY_SCRIPT).exists()


def test_dry_run_writes_nothing(fake_env):  # noqa: F811
    legacy_dir, settings = _legacy_machine(fake_env)
    before = settings.read_bytes()
    lines = []

    assert safety_migrate.migrate(dry_run=True, log=lines.append) == 0

    assert settings.read_bytes() == before and (legacy_dir / LEGACY_SCRIPT).exists()
    assert any("would remove legacy hook script" in line for line in lines), lines
    assert not install.record_path().exists()


def test_migrate_is_human_only(fake_env, monkeypatch, capsys):  # noqa: F811
    legacy_dir, settings = _legacy_machine(fake_env)
    monkeypatch.setattr("agentwire.tasks_cli._host_context_ok", lambda: False)
    before = settings.read_bytes()

    code = safety_migrate.cmd_safety_migrate(SimpleNamespace(dry_run=False, allow_foreign_source=False))

    assert code == 1 and "human-only" in capsys.readouterr().err
    assert settings.read_bytes() == before and (legacy_dir / LEGACY_SCRIPT).exists()
    assert not safety_commands.RULES_DIR.exists() or not list(safety_commands.RULES_DIR.iterdir())
