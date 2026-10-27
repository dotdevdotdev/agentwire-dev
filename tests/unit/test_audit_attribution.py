"""Session attribution on damage-control audit rows + `agentwire safety report`.

The #940 prerequisite: hook-written audit rows carried `session_id: "unknown"`,
so probe traffic could not be separated from real work and no per-session rate
could be computed. Attribution is sourced from the identity #871 already
records (the conversation UUID in the hook stdin payload, resolved to a
session name via tmux or the metadata.json chain) and is FAIL-OPEN — it must
never block an action or crash the hook.

Rule-adjacent subprocess tests run against the BUNDLED rule set (isolated
HOME/AGENTWIRE_DIR, so no live/host rules can leak in) per the
name-the-rule-set-you-measured lesson (#913/#916).
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import HOOKS_DIR


def _load_audit_logger():
    spec = importlib.util.spec_from_file_location(
        "audit_logger_under_test", HOOKS_DIR / "audit_logger.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def audit_logger(monkeypatch, tmp_path):
    mod = _load_audit_logger()
    monkeypatch.setenv("AGENTWIRE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTWIRE_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    mod.HOOK_INPUT = {}
    return mod


def _write_metadata(tmp_path: Path, session: str, conversation_ids):
    d = tmp_path / "sessions" / session
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(json.dumps({"conversation_ids": conversation_ids}))


def _read_rows(tmp_path: Path):
    rows = []
    for f in (tmp_path / "logs" / "damage-control").glob("*.jsonl"):
        rows += [json.loads(line) for line in f.read_text().splitlines()]
    return rows


class TestHookSubprocessAttribution:
    """The executing path: each hook hands its stdin payload to audit_logger.

    Runs against the BUNDLED rules (clean HOME/AGENTWIRE_DIR — nothing under
    ~/.agentwire can leak into the load), so the blocking rule measured here is
    the shipped one.
    """

    def _run_hook(self, hook_file, payload, tmp_path):
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(home),
            "AGENTWIRE_DIR": str(tmp_path),
        }
        return subprocess.run(
            [sys.executable, str(HOOKS_DIR / hook_file)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    def test_bash_block_row_carries_conversation_id(self, tmp_path):
        _write_metadata(tmp_path, "probe-session", ["conv-uuid-1"])
        proc = self._run_hook(
            "bash-tool-damage-control.py",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "session_id": "conv-uuid-1",
            },
            tmp_path,
        )
        assert proc.returncode == 2
        rows = [r for r in _read_rows(tmp_path) if r["decision"] == "blocked"]
        assert rows, "block was not audit-logged"
        assert rows[-1]["conversation_id"] == "conv-uuid-1"
        assert rows[-1]["session_id"] == "probe-session"
