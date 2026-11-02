"""Break-glass: the way out when the venv the hooks run under is gone (#1209 M4).

With ``"onFailure": "block"`` a registered interpreter that no longer exists makes Claude Code
refuse EVERY tool call, including the ones needed to repair it. ``damage-control-breakglass`` is
stdlib-only, lives outside the venv, and is run by a human from an ordinary terminal. These tests
build a REAL venv, register the hooks against it, delete it, and drive the installed script with
a different interpreter.
"""

import json
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from damage_control import claude_code as cc
from damage_control import install

from agentwire import safety_ext
from tests.unit.test_safety_install import fake_env  # noqa: F401  (fixture)

FOREIGN = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/my-own-hook"}]}


def _breakglass(home: Path, *args: str):
    script = install.install_breakglass()
    return subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True, text=True,
                          env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=60)


def _our_entries(home: Path):
    pre = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    return [h for e in pre for h in e["hooks"] if "damage_control.hook" in h.get("args", [])]


@pytest.fixture
def broken_venv(fake_env, tmp_path):  # noqa: F811
    """Hooks registered against a real venv, then the venv deleted: the post-failed-reinstall state."""
    env_dir = tmp_path / "dead-venv"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(env_dir)
    interpreter = str(env_dir / "bin" / "python")
    settings = fake_env / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [FOREIGN]}}))
    safety_ext.install(log=lambda *_: None)
    registered = json.loads(settings.read_text())
    for entry in registered["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            if "damage_control.hook" in hook.get("args", []):
                hook["command"] = interpreter
    settings.write_text(json.dumps(registered))
    shutil.rmtree(env_dir)
    return fake_env, interpreter


def test_the_script_is_copied_out_of_the_package_and_runs_with_no_agentwire(fake_env):  # noqa: F811
    script = install.install_breakglass()
    assert script == install.breakglass_path() and script.stat().st_mode & 0o111
    proc = _breakglass(fake_env, "status")
    assert proc.returncode == 0 and "No damage-control hooks are registered" in proc.stdout


def test_status_names_a_dead_interpreter_and_unregister_restores_a_working_session(broken_venv):
    home, interpreter = broken_venv
    assert len(_our_entries(home)) == len(cc.MATCHERS)
    assert all(h["onFailure"] == "block" for h in _our_entries(home)), "the brick this exists for"

    status = _breakglass(home, "status")
    assert status.returncode == 1 and interpreter in status.stdout and "DEAD" in status.stdout

    done = _breakglass(home, "unregister")
    assert done.returncode == 0 and "DAMAGE CONTROL IS NOW OFF" in done.stdout

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"] == [FOREIGN], "only our entries go; a hook that is not ours stays"
    backup = home / ".claude" / ("settings.json" + ".breakglass.bak")
    assert backup.is_file() and len(json.loads(backup.read_text())["hooks"]["PreToolUse"]) > 1


def test_doctor_flags_the_dead_interpreter_before_the_break_glass_is_needed(broken_venv, capsys):
    from agentwire.doctor_cli import _render_damage_control_section

    home, interpreter = broken_venv
    issues = _render_damage_control_section()
    out = capsys.readouterr().out
    assert issues >= 1 and interpreter in out and "damage-control-breakglass" in out


def test_unregister_refuses_an_unparseable_settings_file_and_leaves_it_untouched(fake_env):  # noqa: F811
    settings = fake_env / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": {oops')
    proc = _breakglass(fake_env, "unregister")
    assert proc.returncode == 1 and "REFUSED" in proc.stdout
    assert settings.read_text() == '{"hooks": {oops'
