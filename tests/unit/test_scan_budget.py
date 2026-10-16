"""The damage-control scan has a budget that fails CLOSED (#1108).

Claude Code kills a PreToolUse hook that outlives its timeout and RUNS the tool
call, so a command that stalls the scan is a bypass of the whole ladder. The
scan must answer inside its budget: ask (never allow) when over it.
"""

import signal
import time
from pathlib import Path

import pytest

import agentwire.safety._core as core
from agentwire.safety._core import check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"


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


def test_ordinary_large_commands_pass_the_size_gate():
    heredoc_commit = "git commit -F - <<'MSG'\n" + "A line of a long commit message body.\n" * 800 + "MSG"
    generated_script = "cat > run.sh <<'EOF'\n" + "echo step && sleep 1  # progress line\n" * 5000 + "EOF"
    json_blob = "curl -d '" + '{"k": "' + "v" * 20_000 + '"}' + "' https://example.com"
    refused = [
        name
        for name, c in {"commit": heredoc_commit, "script": generated_script, "json": json_blob}.items()
        if core._size_budget_refusal(c)
    ]
    assert not refused, refused


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


def test_long_tokens_scan_in_linear_time(cfg):
    """20k chars took 40 s / 12 s before the path prefilter + anchored oddity scan."""
    shapes = {
        "one long word": "echo " + "a" * 20_000,
        "unterminated quotes": "echo " + "'" * 20_000,
        "long word then slash": "echo " + "a" * 20_000 + "/x",
    }
    slow = {}
    for name, c in shapes.items():
        t = time.process_time()
        check_command(c, cfg, deadline_seconds=60)
        elapsed = time.process_time() - t
        if elapsed > 5:
            slow[name] = round(elapsed, 1)
    assert not slow, slow


def test_path_prefilter_keeps_every_spelling_of_a_protected_path_blocked(cfg):
    must_block = {
        "case-insensitive": "echo x > .DamageControl.YML",
        "absolute-prefix": "echo x > /repo/sub/.damagecontrol.yml",
        "tilde-dir": "cp x ~/.agentwire/damage-control/rules.yaml",
        "expanded-dir": f"cp x {Path.home()}/.agentwire/damage-control/rules.yaml",
        "zero-access-glob": "cat service-account-prod.json",
        "zero-access-literal": "cat ~/.ssh/id_rsa",
        "quoted": "echo x > '.agentwire.tasks.yml'",
        "xargs": "echo .agentwire.tasks.yml | xargs rm",
    }
    wrongly_allowed = [n for n, c in must_block.items() if check_command(c, cfg)["decision"] == "allow"]
    assert not wrongly_allowed, wrongly_allowed


def test_registration_writes_an_explicit_timeout_above_the_scan_deadline(tmp_path, monkeypatch):
    import json

    from agentwire import safety_commands

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    safety_commands.register_damage_control_in_settings()
    pre = json.loads((tmp_path / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    timeouts = {h.get("timeout") for entry in pre for h in entry["hooks"]}
    assert timeouts == {core.HOOK_TIMEOUT_SECONDS}
    assert core.SCAN_DEADLINE_SECONDS < core.HOOK_TIMEOUT_SECONDS
