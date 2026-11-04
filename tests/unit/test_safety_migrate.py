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


FRESH_PROCESS_PROBE = """
import json
from agentwire import safety_migrate
from damage_control.engine import policy_file_path
lines = []
code = safety_migrate.migrate(dry_run=True, log=lines.append)
print(json.dumps({"code": code, "lines": lines, "policy": str(policy_file_path())}))
"""


def test_migrate_judges_by_agentwires_profile_in_a_fresh_process(tmp_path):
    """#1268: only importing ``agentwire.safety`` activated the profile, so a bare ``migrate`` read ~/.damage-control.

    A same-process test cannot see this (some earlier import has already activated the profile),
    so the probe runs in a clean interpreter.
    """
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    overlay = home / ".agentwire" / "damage-control"
    overlay.mkdir(parents=True)
    bundled = safety_migrate.safety_ext.RULES_DIR / "agentwire.yaml"
    (overlay / "agentwire.yaml").write_bytes(bundled.read_bytes())  # a pristine copy of a shipped version
    env = {**os.environ, "HOME": str(home), "AGENTWIRE_DIR": str(home / ".agentwire")}
    env.pop("DAMAGE_CONTROL_DIR", None)

    run = subprocess.run([sys.executable, "-I", "-c", FRESH_PROCESS_PROBE], env=env, capture_output=True, text=True)

    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert any("agentwire.yaml" in line for line in out["lines"]), out
    assert out["policy"].startswith(str(home / ".agentwire")), out
    assert not (home / ".damage-control").exists()


def test_a_pristine_copy_of_a_file_the_bundle_no_longer_ships_is_kept(fake_env, monkeypatch):  # noqa: F811
    """#1269: baselines keep retired history, so a shipped hash alone must not retire a copy (gws.yaml, #1143)."""
    overlay = fake_env / ".agentwire" / "damage-control"
    overlay.mkdir(parents=True)
    retired = overlay / "retired.yaml"
    retired.write_text("rules: []\n")
    redundant = overlay / "agentwire.yaml"
    redundant.write_bytes((safety_migrate.safety_ext.RULES_DIR / "agentwire.yaml").read_bytes())
    digest = lambda p: safety_migrate._sha256(p)  # noqa: E731
    monkeypatch.setattr(safety_migrate, "load_baselines", lambda: {
        "rules": {"retired.yaml": [digest(retired)], "agentwire.yaml": [digest(redundant)]},
        "tooldefs": {},
    })
    lines = []

    assert safety_migrate.migrate(log=lines.append) == 0

    assert retired.exists(), "a retired-from-bundle file may be the only protection there is"
    assert not redundant.exists(), "a pristine copy of a still-bundled file is redundant"
    assert any("retired.yaml" in line and "keep" in line for line in lines), lines


def test_doctor_names_unbundled_and_edited_overlays_without_a_fix_line():
    """#1270: migrate keeps these on purpose, so a Fix line would never clear and invites the wrong action."""
    plan = {"rules": {"pristine": [], "edited": ["a.yaml"], "unbundled": ["gws.yaml"]},
            "tooldefs": {"pristine": [], "edited": [], "unbundled": []}}

    lines = safety_migrate.overlay_report_lines(plan)

    assert any("a.yaml" in line and "gws.yaml" in line for line in lines), lines
    assert not [line for line in lines if "Fix" in line], lines


def test_doctor_offers_migrate_only_for_pristine_copies_and_for_their_duplicates():
    clean = {"rules": {"pristine": [], "edited": [], "unbundled": []}}
    legacy = {"rules": {"pristine": ["core.yaml"], "edited": [], "unbundled": []}}

    assert any("Fix: agentwire safety migrate" in line for line in safety_migrate.overlay_report_lines(legacy))
    assert "migrate" in safety_migrate.duplicate_ids_fix(legacy)
    assert "migrate" not in safety_migrate.duplicate_ids_fix(clean)


def test_install_refresh_reaches_the_package_without_prompting(fake_env, monkeypatch):  # noqa: F811
    """#1270: ``--refresh`` must be passed through to ``safety_ext.install`` (and must not ask first)."""
    seen = {}
    monkeypatch.setattr(safety_commands.safety_ext, "install",
                        lambda **kw: seen.update(kw) or SimpleNamespace(code=0))
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(AssertionError("prompted")))

    assert safety_commands.safety_install_cmd(refresh=True) == 0
    assert seen["refresh"] is True
