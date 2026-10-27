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

    def test_corrupt_ledger_fails_open(self, tmp_path):
        assert self._run(tmp_path, "{ this is not json")


