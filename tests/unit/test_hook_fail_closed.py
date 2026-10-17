"""No hook entrypoint can exit non-blocking on a bad or hostile payload (#1108).

A PreToolUse hook that crashes exits 1 and the harness RUNS the tool call, so
every entrypoint must turn any unexpected failure into a blocking exit 2.
"""

import importlib.util
import io
import json
import signal
import sys
from pathlib import Path

import pytest

import agentwire.safety._core as core

pytestmark = pytest.mark.real_agentwire_home

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control"
HOOKS = ("bash", "edit", "write", "read", "mcp")


@pytest.fixture(scope="module")
def hook_modules():
    sys.path.insert(0, str(HOOKS_DIR))
    mods = {}
    for name in HOOKS:
        spec = importlib.util.spec_from_file_location(
            f"dc_hook_{name}", HOOKS_DIR / f"{name}-tool-damage-control.py"
        )
        mods[name] = module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for log in ("log_allowed", "log_asked", "log_blocked", "log_disabled", "log_escape"):
            setattr(module, log, lambda *a, **k: None)  # keep audit rows out of the real ~/.agentwire
    yield mods
    sys.path.remove(str(HOOKS_DIR))


def _exit_code(module, raw, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    try:
        module.run_hook_guarded(module.main)
    except SystemExit as e:
        return e.code
    return 0


GARBAGE = {
    "empty": "",
    "truncated": '{"tool_name": "Bash", "tool_inp',
    "top-level-list": "[1, 2]",
    "top-level-string": '"Bash"',
    "nul-byte": '{"tool_name": "Bash\x00"}',
    "oversized": " " * (core.MAX_HOOK_INPUT_CHARS + 1),
}


def test_every_hook_blocks_on_garbage_input(hook_modules, monkeypatch):
    not_blocked = {
        (hook, name): code
        for hook, module in hook_modules.items()
        for name, raw in GARBAGE.items()
        for code in [_exit_code(module, raw, monkeypatch)]
        if code != 2
    }
    assert not not_blocked, not_blocked


ODD_BUT_PARSEABLE = {
    "deeply-nested": '{"tool_name": "mcp__x__y", "tool_input": ' + "[" * 900 + "]" * 900 + "}",
    "tool-input-not-an-object": '{"tool_name": "%s", "tool_input": [1]}',
}


def test_odd_payloads_never_crash_non_blocking(hook_modules, monkeypatch):
    tool = {"bash": "Bash", "edit": "Edit", "write": "Write", "read": "Read", "mcp": "mcp__x__y"}
    bad = {}
    for hook, module in hook_modules.items():
        for name, raw in ODD_BUT_PARSEABLE.items():
            raw = raw % tool[hook] if "%s" in raw else raw
            code = _exit_code(module, raw, monkeypatch)
            if code not in (0, 2):
                bad[(hook, name)] = code
    assert not bad, bad


def test_an_unexpected_exception_or_escaped_alarm_blocks(hook_modules, monkeypatch):
    def crash(exc):
        def main():
            raise exc

        return main

    not_blocked = {}
    for name, exc in {
        "bug": ValueError("boom"),
        "recursion": RecursionError(),
        "escaped-alarm": core.ScanBudgetExceeded(),
        "ctrl-c": KeyboardInterrupt(),
    }.items():
        try:
            core.run_hook_guarded(crash(exc))
            not_blocked[name] = "returned"
        except SystemExit as e:
            if e.code != 2:
                not_blocked[name] = e.code
    assert not not_blocked, not_blocked


def test_a_deliberate_exit_passes_through_untouched():
    codes = []
    for code in (0, 2):
        try:
            core.run_hook_guarded(lambda c=code: sys.exit(c))
        except SystemExit as e:
            codes.append(e.code)
    assert codes == [0, 2]


def test_large_edit_and_write_payloads_are_not_scanned(hook_modules, monkeypatch):
    big = "x" * (50 << 20)
    payloads = {
        "write": {"tool_name": "Write", "tool_input": {"file_path": "/tmp/scratch.txt", "content": big}},
        "edit": {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/scratch.txt", "old_string": big, "new_string": "y"}},
    }
    codes = {h: _exit_code(hook_modules[h], json.dumps(p), monkeypatch) for h, p in payloads.items()}
    assert codes == {"write": 0, "edit": 0}


def test_mcp_arguments_over_the_screening_budget_ask_unverifiably(hook_modules, monkeypatch, capsys):
    mcp = hook_modules["mcp"]

    def over_budget(*args, **kwargs):
        raise mcp.ScanBudgetExceeded()  # the hook inlines its own copy of the class

    monkeypatch.setattr(mcp, "_screen_tool_paths", over_budget)
    payload = {
        "tool_name": "mcp__agentwire__session_send",
        "tool_input": {"x": "/tmp/a"},
        "permission_mode": "bypassPermissions",
    }
    code = _exit_code(mcp, json.dumps(payload), monkeypatch)
    verdict = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert (code, verdict["permissionDecision"]) == (0, "ask")
    assert "budget" in verdict["permissionDecisionReason"]


def test_mcp_string_walk_survives_deep_nesting_and_repeats(hook_modules):
    mcp = hook_modules["mcp"]
    deep = "/tmp/leaf"
    for _ in range(50_000):
        deep = [deep]
    assert list(mcp._iter_string_values(deep)) == ["/tmp/leaf"]
    assert list(mcp._pathlike_args({"x": ["/tmp/a"] * 100_000})) == ["/tmp/a"]


def test_clock_budget_survives_an_alarm_at_the_worst_moment(monkeypatch):
    """The alarm lands as the guard disarms: still a fail-closed ask, never an escape."""
    real = signal.setitimer
    calls = []

    def fire_on_first_disarm(which, seconds, *rest):
        calls.append(seconds)
        if seconds == 0 and len(calls) == 2:
            raise core.ScanBudgetExceeded()
        return real(which, seconds, *rest)

    monkeypatch.setattr(core, "_decide_command", lambda *a, **k: {"decision": "allow", "reason": "ok"})
    monkeypatch.setattr(core.signal, "setitimer", fire_on_first_disarm)
    result = core.check_command("echo hi", {})
    assert calls == [core.SCAN_DEADLINE_SECONDS, 0, 0]
    assert (result["decision"], result["id"]) == ("ask", "core.scan-budget")
