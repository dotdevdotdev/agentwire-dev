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

import json
from pathlib import Path

import pytest


@pytest.fixture()
def audit_logger(monkeypatch, tmp_path):
    from damage_control import audit as mod

    from agentwire.safety_ext import build_profile

    mod.configure(build_profile())
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

    @pytest.fixture(autouse=True)
    def _hook(self, run_hook):
        self.run_hook = run_hook

    def _run_hook(self, kind, payload, tmp_path):
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        return self.run_hook(
            kind, payload, env={"HOME": str(home), "AGENTWIRE_DIR": str(tmp_path)}, timeout=20,
        )

    def test_bash_block_row_carries_conversation_id(self, tmp_path):
        _write_metadata(tmp_path, "probe-session", ["conv-uuid-1"])
        proc = self._run_hook(
            "bash",
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
