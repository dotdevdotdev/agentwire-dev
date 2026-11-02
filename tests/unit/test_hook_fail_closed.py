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


def _oversized_body(chars):
    """A VALID JSON object of exactly ``chars`` characters."""
    head, tail = '{"tool_name": "Other", "pad": "', '"}'
    return head + "a" * (chars - len(head) - len(tail)) + tail


def test_input_cap_rejects_a_valid_body_one_char_over_and_passes_the_cap(run_hook):
    at_cap, over = (
        _oversized_body(core.MAX_HOOK_INPUT_CHARS),
        _oversized_body(core.MAX_HOOK_INPUT_CHARS + 1),
    )
    ok, refused = run_hook("bash", at_cap), run_hook("bash", over)
    assert (ok.returncode, refused.returncode) == (0, 2)
    assert "too large" in refused.stderr


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
