"""The damage-control scan has a budget that fails CLOSED (#1108).

Claude Code kills a PreToolUse hook that outlives its timeout and RUNS the tool
call, so a command that stalls the scan is a bypass of the whole ladder. The
scan must answer inside its budget: ask (never allow) when over it.
"""

import signal
from pathlib import Path

import damage_control.engine as core
import pytest
from damage_control.engine import bundled_rule_sources, check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = bundled_rule_sources()


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    assert c.get("zeroAccessPaths"), "bundled rules failed to load"
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


def test_oversized_commands_are_asked_not_allowed(cfg):
    oversized = {
        "megabyte-line": "echo " + "a" * (1 << 20),
        "megabyte-of-short-lines": "echo a\n" * (1 << 17),
        "one-long-line-in-a-small-command": "echo ok\n" + "a" * (core.MAX_LINE_CHARS + 1),
        "unterminated-quotes": "echo " + "'" * 100_000,
    }
    wrong = {
        name: (r["decision"], r.get("id"), r.get("unverifiable"))
        for name, c in oversized.items()
        for r in [check_command(c, cfg)]
        if not (r["decision"] == "ask" and r.get("id") == "core.command-too-large" and r.get("unverifiable"))
    }
    assert not wrong, wrong
    assert "too large" in check_command(oversized["megabyte-line"], cfg)["reason"]


def test_clock_budget_fails_closed_and_cleans_up(cfg, monkeypatch):
    allowed = {"decision": "allow", "reason": "stalled scan finished", "pattern": None}

    def stalls(command, config, cwd=None):
        signal.raise_signal(signal.SIGALRM)  # the deadline firing mid-scan
        return allowed

    def stalls_behind_broad_except(command, config, cwd=None):
        try:
            signal.raise_signal(signal.SIGALRM)
        except Exception:  # a broad guard inside the scan must not eat the alarm
            pass
        return allowed

    armed_during_scan = []

    def observes_the_timer(command, config, cwd=None):
        armed_during_scan.append(signal.getitimer(signal.ITIMER_REAL)[0])
        return allowed

    monkeypatch.setattr(core, "_decide_command", observes_the_timer)
    check_command("echo hi", cfg, deadline_seconds=7)
    assert 0 < armed_during_scan[0] <= 7, armed_during_scan

    before = signal.getsignal(signal.SIGALRM)
    outcomes = {}
    for name, fake in {"plain": stalls, "behind-broad-except": stalls_behind_broad_except}.items():
        monkeypatch.setattr(core, "_decide_command", fake)
        r = check_command("echo hi", cfg)
        outcomes[name] = (r["decision"], r.get("id"), r.get("unverifiable"))
    assert outcomes == {
        "plain": ("ask", "core.scan-budget", True),
        "behind-broad-except": ("ask", "core.scan-budget", True),
    }
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is before


def test_registration_sets_the_timeout_on_new_and_existing_entries(tmp_path, monkeypatch):
    import json

    from agentwire import safety_commands

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings_file = tmp_path / ".claude" / "settings.json"

    def timeouts():
        pre = json.loads(settings_file.read_text())["hooks"]["PreToolUse"]
        return {h.get("timeout") for entry in pre for h in entry["hooks"]}

    safety_commands.register_damage_control_in_settings()
    assert timeouts() == {core.HOOK_TIMEOUT_SECONDS}
    assert core.SCAN_DEADLINE_SECONDS < core.HOOK_TIMEOUT_SECONDS
    assert safety_commands.damage_control_timeout_drift() == []

    # a pre-#1108 install: owned entries registered with no timeout (plus a foreign hook that must be left alone)
    settings = json.loads(settings_file.read_text())
    for entry in settings["hooks"]["PreToolUse"]:
        for h in entry["hooks"]:
            h.pop("timeout")
    settings["hooks"]["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/other-hook.sh"}]}
    )
    settings_file.write_text(json.dumps(settings))
    assert set(safety_commands.damage_control_timeout_drift()) == set(safety_commands.DAMAGE_CONTROL_MATCHERS)

    updated = safety_commands.register_damage_control_in_settings()
    assert updated == len(safety_commands.DAMAGE_CONTROL_MATCHERS)
    assert safety_commands.damage_control_timeout_drift() == []
    foreign = [
        h for e in json.loads(settings_file.read_text())["hooks"]["PreToolUse"] for h in e["hooks"]
        if h["command"] == "~/other-hook.sh"
    ]
    assert foreign == [{"type": "command", "command": "~/other-hook.sh"}]
    assert safety_commands.register_damage_control_in_settings() == 0


