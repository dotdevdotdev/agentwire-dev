import json
import stat

import pytest

from agentwire import hooks_cli
from agentwire import safety_commands as sc
from tests.unit.test_safety_heal import fake_env  # noqa: F401  (fixture)


@pytest.mark.real_agentwire_home
def test_the_bash_hook_judges_a_powershell_call_exactly_like_a_bash_call(run_hook):
    def verdict(tool, command):
        return run_hook("bash", {"tool_name": tool, "tool_input": {"command": command}}).returncode

    got = {
        (tool, cmd): verdict(tool, cmd)
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
    assert verdict("Monitor", "rm -rf /agentwire-selftest") == 0


def test_powershell_registers_with_the_bash_hook_and_drift_checks_see_it(fake_env):  # noqa: F811
    assert sc.DAMAGE_CONTROL_MATCHERS["PowerShell"] == sc.DAMAGE_CONTROL_MATCHERS["Bash"]
    sc.register_damage_control_in_settings()
    pre = json.loads((fake_env / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    by_matcher = {e["matcher"]: e["hooks"][0] for e in pre}
    assert by_matcher["PowerShell"] == by_matcher["Bash"]
    assert by_matcher["Bash"]["args"][:4] == ["-I", "-m", "damage_control.hook", "bash"]
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
def test_selftest_probes_the_powershell_path_and_each_hook_kind_once(tmp_path):
    log = tmp_path / "calls.log"
    interpreter = tmp_path / "python-standin"
    # Behaves like the pre-#1218 hook: blocks a destructive Bash call, lets PowerShell through.
    interpreter.write_text(
        "#!/bin/sh\npayload=$(cat)\n"
        f'echo "$payload" >> {log}\n'
        'case "$payload" in *PowerShell*) exit 0;; *"rm -rf"*) exit 2;; esac\n'
        "exit 0\n"
    )
    interpreter.chmod(interpreter.stat().st_mode | stat.S_IXUSR)

    failures = sc.damage_control_selftest(str(interpreter))
    assert len(failures) == 1 and failures[0].startswith("bash:") and "expected 2" in failures[0], failures

    calls = log.read_text().splitlines()
    startup = [ln for ln in calls if "AgentwireSelftest" in ln]
    assert len(startup) == len(set(sc.DAMAGE_CONTROL_MATCHERS.values()))  # shared kind probed once, not per matcher
    assert len(calls) == len(startup) + 2  # plus the Bash and PowerShell destructive probes
