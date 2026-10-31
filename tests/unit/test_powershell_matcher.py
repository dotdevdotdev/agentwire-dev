"""#1218: the PowerShell tool reaches the Bash damage-control hook, and every drift check knows it."""

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

from agentwire import hooks_cli
from agentwire import safety_commands as sc
from tests.unit.test_safety_heal import fake_env  # noqa: F401  (fixture)

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control"


@pytest.fixture(scope="module")
def bash_hook():
    sys.path.insert(0, str(HOOKS_DIR))
    spec = importlib.util.spec_from_file_location("dc_hook_ps_bash", HOOKS_DIR / "bash-tool-damage-control.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for log in ("log_allowed", "log_asked", "log_blocked", "log_disabled", "log_escape"):
        setattr(module, log, lambda *a, **k: None)  # keep audit rows out of the real ~/.agentwire
    yield module
    sys.path.remove(str(HOOKS_DIR))


@pytest.fixture(autouse=True)
def exits_raise(monkeypatch):
    """The guard ends the process with os._exit; surface it as SystemExit for the test."""

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(os, "_exit", fake_exit)


def _exit(module, tool, command, monkeypatch):
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    try:
        module.run_hook_guarded(module.main)
    except SystemExit as e:
        return e.code
    return "returned"


@pytest.mark.real_agentwire_home
def test_the_bash_hook_judges_a_powershell_call_exactly_like_a_bash_call(bash_hook, monkeypatch):
    got = {
        (tool, cmd): _exit(bash_hook, tool, cmd, monkeypatch)
        for tool in ("Bash", "PowerShell")
        for cmd in ("rm -rf /agentwire-selftest", "echo hi")
    }
    assert got == {
        ("Bash", "rm -rf /agentwire-selftest"): 2,
        ("PowerShell", "rm -rf /agentwire-selftest"): 2,
        ("Bash", "echo hi"): 0,
        ("PowerShell", "echo hi"): 0,
    }
    # a tool the hook does not gate is still ignored
    assert _exit(bash_hook, "Monitor", "rm -rf /agentwire-selftest", monkeypatch) == 0


def test_powershell_registers_with_the_bash_hook_and_drift_checks_see_it(fake_env):  # noqa: F811
    assert sc.DAMAGE_CONTROL_MATCHERS["PowerShell"] == sc.DAMAGE_CONTROL_MATCHERS["Bash"]
    sc.register_damage_control_in_settings()
    pre = json.loads((fake_env / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    bash_command = f"~/.agentwire/hooks/damage-control/{sc.DAMAGE_CONTROL_MATCHERS['Bash']}"
    by_matcher = {e["matcher"]: e["hooks"][0]["command"] for e in pre}
    assert by_matcher["PowerShell"] == by_matcher["Bash"] == bash_command
    assert (sc.missing_damage_control_matchers(), sc.damage_control_timeout_drift(),
            sc.damage_control_onfailure_drift()) == ([], [], [])

    # An install that predates #1218 has Bash but no PowerShell: reported missing, then healed,
    # and the shared command must not make the Bash entry stand in for the PowerShell one.
    path = fake_env / ".claude" / "settings.json"
    settings = json.loads(path.read_text())
    settings["hooks"]["PreToolUse"] = [e for e in settings["hooks"]["PreToolUse"] if e["matcher"] != "PowerShell"]
    path.write_text(json.dumps(settings))
    assert sc.missing_damage_control_matchers() == ["PowerShell"]
    assert sc.register_damage_control_in_settings() == 1
    assert sc.missing_damage_control_matchers() == []


def test_the_outcome_logger_matches_the_powershell_tool_too():
    assert "PowerShell" in hooks_cli.hook_matcher("outcome_logger.py").split("|")


@pytest.mark.real_hook_probe
def test_selftest_probes_the_powershell_path_and_each_hook_file_once(tmp_path, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    log = tmp_path / "calls.log"
    script = (
        "#!/bin/sh\npayload=$(cat)\n"
        f'echo "$(basename "$0") $payload" >> {log}\n'
        'case "$payload" in *PowerShell*) exit 0;; *"rm -rf"*) [ "$(basename "$0")" = bash-tool-damage-control.py ] && exit 2;; esac\n'
        "exit 0\n"
    )
    for name in set(sc.DAMAGE_CONTROL_MATCHERS.values()):
        (hooks / name).write_text(script)
        (hooks / name).chmod(0o755)
    monkeypatch.setattr(sc, "HOOKS_DIR", hooks)

    # this stand-in lets PowerShell through, as the pre-#1218 hook did: the selftest must notice
    failures = sc.damage_control_selftest()
    assert any("bash-tool-damage-control.py" in f and "expected 2" in f for f in failures), failures

    startup = [ln for ln in log.read_text().splitlines() if "AgentwireSelftest" in ln]
    assert len(startup) == len(set(sc.DAMAGE_CONTROL_MATCHERS.values()))  # shared file probed once, not per matcher
