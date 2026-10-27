"""Phase 0 of #1068: record what happened AFTER damage-control decided.

Covers the three moving parts together: decision rows carry the join key,
the outcome hook writes content-free rows and can never break a tool call, and
the join turns (decision, outcome) pairs into the labels rule tuning counts.
"""

import json
import subprocess
import sys

from agentwire.hooks_cli import get_hooks_source

OUTCOME_HOOK = get_hooks_source() / "outcome_logger.py"


def _run_hook(payload, tmp_path, raw=None):
    return subprocess.run(
        [sys.executable, str(OUTCOME_HOOK)],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env={"AGENTWIRE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )


def _rows(tmp_path):
    out = []
    for f in sorted((tmp_path / "logs" / "damage-control").glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


def test_outcome_hook_writes_a_content_free_row_and_never_fails(tmp_path):
    secret = "SECRET-COMMAND-TEXT"
    proc = _run_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_use_id": "toolu_1",
            "tool_name": "Bash",
            "session_id": "conv-1",
            "permission_mode": "default",
            "cwd": "/repo",
            "duration_ms": 12,
            "tool_input": {"command": secret},
            "tool_response": {"stdout": secret, "interrupted": False},
        },
        tmp_path,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    (row,) = _rows(tmp_path)
    assert (row["kind"], row["result"], row["tool_use_id"], row["duration_ms"]) == (
        "outcome",
        "executed",
        "toolu_1",
        12,
    )
    assert row["interrupted"] is False
    assert secret not in json.dumps(row)

    bad = {
        "not-json": _run_hook(None, tmp_path, raw="{{{"),
        "empty": _run_hook(None, tmp_path, raw=""),
        "list": _run_hook(None, tmp_path, raw="[1,2]"),
        "no-event": _run_hook({"tool_use_id": "t"}, tmp_path),
    }
    broken = [n for n, p in bad.items() if (p.returncode, p.stdout, p.stderr) != (0, "", "")]
    assert not broken, broken
