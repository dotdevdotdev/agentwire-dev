"""No hook entrypoint can exit non-blocking on a bad or hostile payload (#1108).

A PreToolUse hook that crashes exits 1 and the harness RUNS the tool call, so
every entrypoint must turn any unexpected failure into a blocking exit 2. The
converse holds too: an AUDIT-LOG failure must never change a decision.
"""

import os

import damage_control.engine as core
import pytest

pytestmark = pytest.mark.real_agentwire_home


@pytest.fixture(autouse=True)
def exits_raise(monkeypatch):
    """The guard ends the process with os._exit; surface it as SystemExit for the test."""

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(os, "_exit", fake_exit)


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
