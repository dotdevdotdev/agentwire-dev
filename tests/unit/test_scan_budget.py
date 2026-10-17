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
        "long quoted string": "echo '" + "x" * 20_000 + "'",
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


def test_the_alarm_interrupts_a_real_pathological_scan(cfg):
    """No stub: a scan that takes seconds is cut off at the deadline and asks."""
    pathological = "echo '" + "../" * 10_000 + "'"  # ~8 s unbounded
    t = time.perf_counter()
    result = check_command(pathological, cfg, deadline_seconds=0.3)
    elapsed = time.perf_counter() - t
    assert (result["decision"], result.get("id")) == ("ask", "core.scan-budget")
    assert elapsed < 3, elapsed


def test_the_kill_switch_turns_the_budget_off_too(cfg):
    off = {**cfg, "safety": {"enabled": False, "disabled_rules": []}}
    big_line = "echo " + "a" * 40_000
    assert check_command(big_line, cfg)["id"] == "core.command-too-large"
    result = check_command(big_line, off)
    assert (result["decision"], result.get("disabled")) == ("allow", True)

    # a scan that outruns the clock is allowed (disabled), not asked
    slow = "echo '" + "../" * 10_000 + "'"
    assert check_command(slow, cfg, deadline_seconds=0.3)["id"] == "core.scan-budget"
    assert check_command(slow, off, deadline_seconds=0.3)["decision"] == "allow"

    # the control plane precedes the switch by design: it still blocks
    assert check_command("echo x > ~/.agentwire/damagecontrol.yml", off)["decision"] == "block"


def test_a_corrupt_damagecontrol_file_keeps_damage_control_on(tmp_path):
    corrupt = tmp_path / "damagecontrol.yml"
    corrupt.write_text("enabled: [unclosed\n  : :")
    assert core.load_safety_config(global_config_path=corrupt, cwd=str(tmp_path))["enabled"] is True


def test_the_hooks_honor_the_kill_switch_interactive_and_unattended(monkeypatch, capsys):
    import importlib.util
    import io
    import json
    import os
    import sys

    hooks_dir = RULES_DIR.parent
    sys.path.insert(0, str(hooks_dir))
    try:
        mods = {}
        for name in ("bash", "mcp"):
            spec = importlib.util.spec_from_file_location(f"kill_{name}", hooks_dir / f"{name}-tool-damage-control.py")
            mods[name] = mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for log in ("log_allowed", "log_asked", "log_blocked", "log_disabled", "log_escape"):
                setattr(mod, log, lambda *a, **k: None)
    finally:
        sys.path.remove(str(hooks_dir))

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(os, "_exit", fake_exit)

    def run(mod, payload, enabled, unattended):
        monkeypatch.setattr(mod, "load_safety_config", lambda: {"enabled": enabled, "disabled_rules": []})
        monkeypatch.setattr(mod, "is_unattended", lambda: unattended)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        capsys.readouterr()
        try:
            mod.run_hook_guarded(mod.main)
        except SystemExit as e:
            return e.code, bool(capsys.readouterr().out)

    bash = {"tool_name": "Bash", "tool_input": {"command": "echo " + "a" * 40_000}}

    def over_budget(*args, **kwargs):
        raise mods["mcp"].ScanBudgetExceeded()

    monkeypatch.setattr(mods["mcp"], "_screen_tool_paths", over_budget)
    mcp = {"tool_name": "mcp__agentwire__session_send", "tool_input": {"x": "/tmp/a"}}
    seen = {
        (hook, enabled, unattended): run(mods[hook], payload, enabled, unattended)
        for hook, payload in (("bash", bash), ("mcp", mcp))
        for enabled in (False, True)
        for unattended in (False, True)
    }
    # (exit code, printed an ask)
    assert seen == {
        ("bash", False, False): (0, False), ("bash", False, True): (0, False),
        ("bash", True, False): (0, True), ("bash", True, True): (2, False),
        ("mcp", False, False): (0, False), ("mcp", False, True): (0, False),
        ("mcp", True, False): (0, True), ("mcp", True, True): (2, False),
    }


def test_malformed_settings_never_crash_registration_or_doctor(tmp_path, monkeypatch):
    import json

    from agentwire import safety_commands

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings_file = tmp_path / ".claude" / "settings.json"
    settings_file.parent.mkdir()
    shapes = {
        "hooks-list": {"hooks": []},
        "pre-dict": {"hooks": {"PreToolUse": {}}},
        "entry-hooks-empty-list": {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}},
        "entry-hooks-not-list": {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": {}}]}},
        "entry-not-dict": {"hooks": {"PreToolUse": ["Bash", None, 3]}},
        "inner-not-dict": {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": ["x", 1]}]}},
        "top-level-list": [],
    }
    crashed = {}
    for name, shape in shapes.items():
        settings_file.write_text(json.dumps(shape))
        try:
            safety_commands.damage_control_timeout_drift()
            safety_commands.missing_damage_control_matchers()
            safety_commands.register_damage_control_in_settings()
        except Exception as e:  # noqa: BLE001 - collecting every offender
            crashed[name] = repr(e)
    assert not crashed, crashed


QUOTED_WRITERS = {
    "redirect": "echo x > {p}",
    "rm": "rm {p}",
    "cp": "cp x {p}",
    "tee": "echo x | tee {p}",
    "sed-i": "sed -i.bak s/a/b/ {p}",
    "mv": "mv x {p}",
    "ln": "ln -sf x {p}",
    "curl-o": "curl -o {p} http://e.com",
    "dd": "dd if=x of={p}",
    "python": "python3 -c \"open('{p}','w').write('x')\"",
}


def test_a_long_quoted_string_never_hides_a_writer_next_to_a_protected_path(cfg):
    """The quoted-string linearization must not drop a writer: all variants stay blocked.

    Differential against main over this slice (3K and 31K pads) found 0 decision
    differences; the only allows are a writer and path BOTH inside one quoted
    string, which is text, on main too.
    """
    pad = "x" * 3000
    control_plane = "~/.agentwire/damagecontrol.yml"
    variants = {
        "outside-balanced": lambda w: f"echo '{pad}'; " + w.format(p=control_plane),
        "outside-unbalanced": lambda w: f"echo '{pad}; " + w.format(p=control_plane),
        "outside-double-quoted": lambda w: f'echo "{pad}"; ' + w.format(p=control_plane),
        "outside-after-pathy-pad": lambda w: f'echo "{"a/b/" * 750}"; ' + w.format(p=control_plane),
        "path-in-quote-balanced": lambda w: w.format(p=f"'{pad}/.damagecontrol.yml'"),
        "path-in-quote-unbalanced": lambda w: w.format(p=f"'{pad}/.damagecontrol.yml"),
    }
    not_blocked = [
        f"{writer}/{variant}"
        for writer, template in QUOTED_WRITERS.items()
        for variant, build in variants.items()
        if check_command(build(template), cfg)["decision"] != "block"
    ]
    assert not not_blocked, not_blocked
