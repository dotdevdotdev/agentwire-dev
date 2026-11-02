import pytest

from agentwire import hooks_cli


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


def test_the_outcome_logger_matches_the_powershell_tool_too():
    assert "PowerShell" in hooks_cli.hook_matcher("outcome_logger.py").split("|")
