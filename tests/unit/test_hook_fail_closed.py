"""No hook entrypoint can exit non-blocking on a bad or hostile payload (#1108).

A PreToolUse hook that crashes exits 1 and the harness RUNS the tool call, so
every entrypoint must turn any unexpected failure into a blocking exit 2. The
converse holds too: an AUDIT-LOG failure must never change a decision.
"""

import importlib.util
import io
import os
import sys
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
            setattr(
                module, log, lambda *a, **k: None
            )  # keep audit rows out of the real ~/.agentwire
    yield mods
    sys.path.remove(str(HOOKS_DIR))


@pytest.fixture(autouse=True)
def exits_raise(monkeypatch):
    """The guard ends the process with os._exit; surface it as SystemExit for the test."""

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(os, "_exit", fake_exit)


def _run(module, raw, monkeypatch, capsys=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    try:
        module.run_hook_guarded(module.main)
    except SystemExit as e:
        return e.code
    return "returned"


def _exit_code(module, raw, monkeypatch):
    return _run(module, raw, monkeypatch)


def _oversized_body(chars):
    """A VALID JSON object of exactly ``chars`` characters."""
    head, tail = '{"tool_name": "Other", "pad": "', '"}'
    return head + "a" * (chars - len(head) - len(tail)) + tail


def test_input_cap_rejects_a_valid_body_one_char_over_and_passes_the_cap(
    hook_modules, monkeypatch, capsys
):
    bash = hook_modules["bash"]
    at_cap, over = (
        _oversized_body(core.MAX_HOOK_INPUT_CHARS),
        _oversized_body(core.MAX_HOOK_INPUT_CHARS + 1),
    )
    capsys.readouterr()
    codes = (_exit_code(bash, at_cap, monkeypatch), _exit_code(bash, over, monkeypatch))
    err = capsys.readouterr().err
    assert codes == (0, 2)
    assert "too large" in err


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


# ---- an audit-log failure never changes the decision -------------------------
