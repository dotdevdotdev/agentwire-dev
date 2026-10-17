"""No hook entrypoint can exit non-blocking on a bad or hostile payload (#1108).

A PreToolUse hook that crashes exits 1 and the harness RUNS the tool call, so
every entrypoint must turn any unexpected failure into a blocking exit 2. The
converse holds too: an AUDIT-LOG failure must never change a decision.
"""

import errno
import importlib.util
import io
import json
import os
import signal
import sys
import time
import types
from pathlib import Path

import pytest

import agentwire.safety._core as core

pytestmark = pytest.mark.real_agentwire_home

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control"
HOOKS = ("bash", "edit", "write", "read", "mcp")
LOG_FUNCS = ("log_allowed", "log_asked", "log_blocked", "log_disabled", "log_escape")


def _load_hook(name):
    spec = importlib.util.spec_from_file_location(
        f"dc_hook_{name}", HOOKS_DIR / f"{name}-tool-damage-control.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hook_modules():
    sys.path.insert(0, str(HOOKS_DIR))
    mods = {name: _load_hook(name) for name in HOOKS}
    for module in mods.values():
        for log in LOG_FUNCS:
            setattr(module, log, lambda *a, **k: None)  # keep audit rows out of the real ~/.agentwire
    yield mods
    sys.path.remove(str(HOOKS_DIR))


@pytest.fixture(autouse=True)
def exits_raise(monkeypatch):
    """The guard ends the process with os._exit; surface it as SystemExit for the test."""

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(os, "_exit", fake_exit)


class DeadPipe(io.TextIOBase):
    """A stream whose reader has gone away."""

    def write(self, s):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def close(self):  # IOBase.__del__ would flush a dead pipe
        pass


def _run(module, raw, monkeypatch, capsys=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    try:
        module.run_hook_guarded(module.main)
    except SystemExit as e:
        return e.code
    return "returned"


def _exit_code(module, raw, monkeypatch):
    return _run(module, raw, monkeypatch)


GARBAGE = {
    "empty": ("", "invalid JSON"),
    "truncated": ('{"tool_name": "Bash", "tool_inp', "invalid JSON"),
    "top-level-list": ("[1, 2]", "not a JSON object"),
    "top-level-string": ('"Bash"', "not a JSON object"),
    "nul-byte": ('{"tool_name": "Bash\x00"}', "invalid JSON"),
    "deeply-nested": ("[" * 100_000, "nested too deeply"),
}


def test_every_hook_blocks_on_garbage_input_and_says_why(hook_modules, monkeypatch, capsys):
    wrong = {}
    for hook, module in hook_modules.items():
        for name, (raw, phrase) in GARBAGE.items():
            capsys.readouterr()
            code = _exit_code(module, raw, monkeypatch)
            err = capsys.readouterr().err
            if code != 2 or phrase not in err:
                wrong[(hook, name)] = (code, err[:120])
    assert not wrong, wrong


def _oversized_body(chars):
    """A VALID JSON object of exactly ``chars`` characters."""
    head, tail = '{"tool_name": "Other", "pad": "', '"}'
    return head + "a" * (chars - len(head) - len(tail)) + tail


def test_input_cap_rejects_a_valid_body_one_char_over_and_passes_the_cap(hook_modules, monkeypatch, capsys):
    bash = hook_modules["bash"]
    at_cap, over = _oversized_body(core.MAX_HOOK_INPUT_CHARS), _oversized_body(core.MAX_HOOK_INPUT_CHARS + 1)
    capsys.readouterr()
    codes = (_exit_code(bash, at_cap, monkeypatch), _exit_code(bash, over, monkeypatch))
    err = capsys.readouterr().err
    assert codes == (0, 2)
    assert "too large" in err


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


def test_an_unexpected_exception_or_escaped_alarm_blocks(monkeypatch):
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


def test_the_exit_code_survives_closed_pipes(monkeypatch):
    """A dead stderr must not turn a block into 120; a dead stdout can't carry an allow."""
    outcomes = {}
    for name, (stdout, stderr, hook_exit) in {
        "block-with-dead-stderr": (io.StringIO(), DeadPipe(), 2),
        "block-with-both-dead": (DeadPipe(), DeadPipe(), 2),
        "allow-with-dead-stderr": (io.StringIO(), DeadPipe(), 0),
        "allow-with-dead-stdout": (DeadPipe(), io.StringIO(), 0),
    }.items():
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)
        try:
            core.run_hook_guarded(lambda c=hook_exit: sys.exit(c))
        except SystemExit as e:
            outcomes[name] = e.code
    assert outcomes == {
        "block-with-dead-stderr": 2,
        "block-with-both-dead": 2,
        "allow-with-dead-stderr": 0,
        "allow-with-dead-stdout": 2,
    }


def test_large_edit_and_write_payloads_are_not_scanned(hook_modules, monkeypatch):
    big = "x" * (50 << 20)
    payloads = {
        "write": {"tool_name": "Write", "tool_input": {"file_path": "/tmp/scratch.txt", "content": big}},
        "edit": {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/scratch.txt", "old_string": big, "new_string": "y"}},
    }
    codes = {h: _exit_code(hook_modules[h], json.dumps(p), monkeypatch) for h, p in payloads.items()}
    assert codes == {"write": 0, "edit": 0}


def _mcp_payload(tool_input, mode="bypassPermissions"):
    return json.dumps({
        "tool_name": "mcp__agentwire__session_send",
        "tool_input": tool_input,
        "permission_mode": mode,
    })


def test_mcp_deadline_is_wired_to_the_real_screen(hook_modules, monkeypatch, capsys):
    """No stub: 100k unique paths take seconds to screen; a 0.3 s deadline cuts it to an ask."""
    mcp = hook_modules["mcp"]
    monkeypatch.setattr(mcp, "SCAN_DEADLINE_SECONDS", 0.3)
    payload = _mcp_payload({"x": [f"/tmp/f{i}" for i in range(100_000)]})
    t = time.perf_counter()
    code = _exit_code(mcp, payload, monkeypatch)
    elapsed = time.perf_counter() - t
    verdict = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert (code, verdict["permissionDecision"]) == (0, "ask")
    assert "budget" in verdict["permissionDecisionReason"]
    assert elapsed < 3, elapsed


def test_mcp_arguments_over_the_screening_budget_ask_unverifiably(hook_modules, monkeypatch, capsys):
    mcp = hook_modules["mcp"]

    def over_budget(*args, **kwargs):
        raise mcp.ScanBudgetExceeded()  # the hook inlines its own copy of the class

    monkeypatch.setattr(mcp, "_screen_tool_paths", over_budget)
    code = _exit_code(mcp, _mcp_payload({"x": "/tmp/a"}), monkeypatch)
    verdict = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert (code, verdict["permissionDecision"]) == (0, "ask")
    assert "budget" in verdict["permissionDecisionReason"]


def test_mcp_over_budget_with_the_switch_off_asks_only_for_control_plane_paths(hook_modules, monkeypatch, capsys):
    mcp = hook_modules["mcp"]

    def over_budget(*args, **kwargs):
        raise mcp.ScanBudgetExceeded()

    monkeypatch.setattr(mcp, "_screen_tool_paths", over_budget)
    monkeypatch.setattr(mcp, "load_safety_config", lambda: {"enabled": False, "disabled_rules": []})
    outcomes = {}
    for name, args in {
        "plain": {"x": ["/tmp/a"]},
        "control-plane": {"x": [{"deep": ["~/.agentwire/damagecontrol.yml"]}]},
        "home-var": {"x": "$HOME/.agentwire/damagecontrol.yml"},
    }.items():
        capsys.readouterr()
        code = _exit_code(mcp, _mcp_payload(args), monkeypatch)
        outcomes[name] = (code, bool(capsys.readouterr().out))
    assert outcomes == {"plain": (0, False), "control-plane": (0, True), "home-var": (0, True)}


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


# ---- an audit-log failure never changes the decision -------------------------


@pytest.fixture
def failing_audit(monkeypatch):
    """Hooks loaded against an audit_logger whose every write raises ENOSPC."""

    def full_disk(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    fake = types.ModuleType("audit_logger")
    for log in LOG_FUNCS:
        setattr(fake, log, full_disk)
    monkeypatch.setitem(sys.modules, "audit_logger", fake)
    return {name: _load_hook(name) for name in HOOKS}


AUDIT_CASES = {
    # hook: (payload that is allowed, payload that is blocked, phrase of the REAL block reason)
    "bash": (
        {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
        {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}},
        "force",
    ),
    "write": (
        {"tool_name": "Write", "tool_input": {"file_path": "/tmp/scratch.txt", "content": "x"}},
        {"tool_name": "Write", "tool_input": {"file_path": "~/.ssh/id_rsa", "content": "x"}},
        "ssh",
    ),
    "edit": (
        {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/scratch.txt", "old_string": "a", "new_string": "b"}},
        {"tool_name": "Edit", "tool_input": {"file_path": "~/.ssh/id_rsa", "old_string": "a", "new_string": "b"}},
        "ssh",
    ),
    "read": (
        {"tool_name": "Read", "tool_input": {"file_path": "/tmp/scratch.txt"}},
        {"tool_name": "Read", "tool_input": {"file_path": "~/.ssh/id_rsa"}},
        "ssh",
    ),
    "mcp": (
        {"tool_name": "mcp__x__y", "tool_input": {"note": "hello"}},
        {"tool_name": "mcp__x__y", "tool_input": {"p": "~/.ssh/id_rsa"}},
        "ssh",
    ),
}


def test_an_audit_log_failure_never_changes_the_decision(failing_audit, monkeypatch, capsys):
    wrong = {}
    for hook, (allowed, blocked, _) in AUDIT_CASES.items():
        for dead_stderr in (False, True):
            capsys.readouterr()
            if dead_stderr:
                monkeypatch.setattr(sys, "stderr", DeadPipe())
            code_ok = _exit_code(failing_audit[hook], json.dumps(allowed), monkeypatch)
            code_blocked = _exit_code(failing_audit[hook], json.dumps(blocked), monkeypatch)
            if (code_ok, code_blocked) != (0, 2):
                wrong[(hook, "dead-stderr" if dead_stderr else "stderr-ok")] = (code_ok, code_blocked)
    assert not wrong, wrong


def test_a_blocked_call_keeps_its_real_reason_when_the_audit_log_fails(failing_audit, monkeypatch, capsys):
    wrong = {}
    for hook, (_, blocked, phrase) in AUDIT_CASES.items():
        capsys.readouterr()
        code = _exit_code(failing_audit[hook], json.dumps(blocked), monkeypatch)
        err = capsys.readouterr().err
        if code != 2 or phrase not in err.lower() or "failing closed" in err:
            wrong[hook] = (code, err[:200])
    assert not wrong, wrong
