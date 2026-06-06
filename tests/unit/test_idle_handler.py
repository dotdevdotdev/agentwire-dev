"""Tests for agentwire/hooks/idle-handler.sh — boolean task-context reads.

The hook reads booleans from the task-context JSON with jq. jq's // operator
coerces a stored ``false`` to the right-hand default (``false // true`` evaluates
to ``true``), which silently disabled ``exit_on_complete: false`` and killed
persistent sessions after every scheduled run (issue #234).

These tests extract the *actual* jq filters from the hook source and run them
through real jq, so a regression back to the ``//`` idiom fails the suite.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


HOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "agentwire" / "hooks" / "idle-handler.sh"
)

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed"
)


def _extract_jq_filter(var_name: str) -> str:
    """Pull the jq filter the hook uses to read ``var_name`` from the context file."""
    source = HOOK_PATH.read_text()
    match = re.search(rf"^\s*{var_name}=\$\(jq -r '([^']+)'", source, re.MULTILINE)
    assert match, f"could not find jq read for {var_name} in idle-handler.sh"
    return match.group(1)


def _run_jq(jq_filter: str, payload: dict) -> str:
    result = subprocess.run(
        ["jq", "-r", jq_filter],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("var_name", ["exit_on_complete", "loop_review"])
class TestBooleanContextReads:
    """Stored booleans must round-trip; only null/absent falls back to the default."""

    def test_false_is_preserved(self, var_name):
        # The #234 regression: jq's // coerced a stored false to true.
        jq_filter = _extract_jq_filter(var_name)
        assert _run_jq(jq_filter, {var_name: False}) == "false"

    def test_true_is_preserved(self, var_name):
        jq_filter = _extract_jq_filter(var_name)
        assert _run_jq(jq_filter, {var_name: True}) == "true"

    def test_absent_defaults_to_true(self, var_name):
        jq_filter = _extract_jq_filter(var_name)
        assert _run_jq(jq_filter, {}) == "true"

    def test_null_defaults_to_true(self, var_name):
        jq_filter = _extract_jq_filter(var_name)
        assert _run_jq(jq_filter, {var_name: None}) == "true"


class TestSecondIdleCleanup:
    """The context file must be removed on BOTH exit_on_complete branches —
    ensure's completion poll blocks until the file is gone."""

    def test_context_removed_when_session_left_alive(self):
        lines = HOOK_PATH.read_text().splitlines()
        marker = [i for i, l in enumerate(lines)
                  if "exit_on_complete=false, session left alive" in l]
        assert marker, "persistent branch (exit_on_complete=false) missing from hook"
        preceding = "\n".join(lines[max(0, marker[0] - 6):marker[0]])
        assert 'rm "$task_context_file"' in preceding, (
            "persistent branch must remove $task_context_file — "
            "wait_for_completion_signal blocks until the context file is deleted"
        )
