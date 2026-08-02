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
import time
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
        marker = [i for i, line in enumerate(lines)
                  if "exit_on_complete=false, session left alive" in line]
        assert marker, "persistent branch (exit_on_complete=false) missing from hook"
        preceding = "\n".join(lines[max(0, marker[0] - 6):marker[0]])
        assert 'rm "$task_context_file"' in preceding, (
            "persistent branch must remove $task_context_file — "
            "wait_for_completion_signal blocks until the context file is deleted"
        )


class TestUsageLimitParkGuard:
    """A session parked on a usage limit (#274) must short-circuit the hook —
    no summary prompts, no /exit, no kill — before ANY idle handling runs."""

    def test_guard_exists_and_exits_zero(self):
        source = HOOK_PATH.read_text()
        guard = source.find('usage-limit/${tmux_session}.json')
        assert guard != -1, "usage-limit park guard missing from idle-handler.sh"
        following = source[guard:guard + 300]
        assert "exit 0" in following, "park guard must exit 0, not fall through"

    def test_guard_runs_before_all_idle_handling(self):
        source = HOOK_PATH.read_text()
        guard = source.find('usage-limit/${tmux_session}.json')
        worker_branch = source.find("Worker pane detected")
        task_branch = source.find("task_context_file=")
        assert guard != -1 and worker_branch != -1 and task_branch != -1
        assert guard < worker_branch, "guard must precede worker-pane handling"
        assert guard < task_branch, "guard must precede scheduled-task handling"


class TestCohortGuard:
    """#852 — a parent with outstanding fan-out children is WAITING, not done.

    The guard block is extracted from the hook and executed for real, so the
    deadline arithmetic and the jq reads are exercised rather than grepped.
    """

    @staticmethod
    def _guard_block() -> str:
        lines = HOOK_PATH.read_text().splitlines()
        start = next(i for i, ln in enumerate(lines) if "cohort_file=" in ln)
        depth = 0
        for end in range(start, len(lines)):
            stripped = lines[end].strip()
            if stripped.startswith("if "):
                depth += 1
            elif stripped == "fi":
                depth -= 1
                if depth == 0:
                    return "\n".join(lines[start:end + 1])
        raise AssertionError("cohort guard block is unterminated")

    def _run(self, tmp_path, ledger: dict | str | None, session="memory-manager"):
        """Run the guard with a fake HOME. Returns True if idle handling continues."""
        if ledger is not None:
            path = tmp_path / ".agentwire" / "cohorts"
            path.mkdir(parents=True, exist_ok=True)
            body = ledger if isinstance(ledger, str) else json.dumps(ledger)
            (path / f"{session}.json").write_text(body)
        script = (
            "log() { :; }\n"
            f'tmux_session="{session}"\n'
            f"{self._guard_block()}\n"
            "echo CONTINUED\n"
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return "CONTINUED" in result.stdout

    def _ledger(self, states, deadline_offset=3600):
        return {
            "parent": "memory-manager",
            "deadline": int(time.time()) + deadline_offset,
            "children": [{"session": f"c{i}", "state": s, "report": None}
                         for i, s in enumerate(states)],
        }

    def test_pending_children_suppress_idle_handling(self, tmp_path):
        assert not self._run(tmp_path, self._ledger(["pending", "reported"]))

    def test_resolved_cohort_resumes_normal_handling(self, tmp_path):
        assert self._run(tmp_path, self._ledger(["reported", "gone", "timeout"]))

    def test_past_deadline_resumes_normal_handling(self, tmp_path):
        # Bounded: a wedged child cannot pin a task alive forever.
        assert self._run(tmp_path, self._ledger(["pending"], deadline_offset=-1))

    def test_no_ledger_is_untouched(self, tmp_path):
        assert self._run(tmp_path, None)

    def test_corrupt_ledger_fails_open(self, tmp_path):
        assert self._run(tmp_path, "{ this is not json")

    def test_missing_fields_fail_open(self, tmp_path):
        assert self._run(tmp_path, {"parent": "memory-manager"})

    def test_guard_runs_before_all_idle_handling(self):
        source = HOOK_PATH.read_text()
        guard = source.find("cohort_file=")
        assert guard != -1, "cohort guard missing from idle-handler.sh"
        assert guard < source.find("Worker pane detected")
        assert guard < source.find("task_context_file=")
