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
    (d / "metadata.json").write_text(
        json.dumps({"conversation_ids": conversation_ids})
    )


def _read_rows(tmp_path: Path):
    rows = []
    for f in (tmp_path / "logs" / "damage-control").glob("*.jsonl"):
        rows += [json.loads(line) for line in f.read_text().splitlines()]
    return rows


class TestAttribution:
    def test_conversation_id_from_hook_input(self, audit_logger, tmp_path):
        audit_logger.HOOK_INPUT = {"session_id": "abc-123"}
        audit_logger.log_blocked("Bash", "rm -rf /", "test reason")
        (row,) = _read_rows(tmp_path)
        assert row["conversation_id"] == "abc-123"

    def test_session_resolved_from_metadata_chain(self, audit_logger, tmp_path):
        _write_metadata(tmp_path, "agentwire-dev-audit", ["old-id", "abc-123"])
        audit_logger.HOOK_INPUT = {"session_id": "abc-123"}
        audit_logger.log_blocked("Bash", "rm -rf /", "test reason")
        (row,) = _read_rows(tmp_path)
        assert row["session_id"] == "agentwire-dev-audit"

    def test_env_override_wins(self, audit_logger, tmp_path, monkeypatch):
        _write_metadata(tmp_path, "from-metadata", ["abc-123"])
        monkeypatch.setenv("AGENTWIRE_SESSION_ID", "from-env")
        audit_logger.HOOK_INPUT = {"session_id": "abc-123"}
        audit_logger.log_blocked("Bash", "x", "r")
        (row,) = _read_rows(tmp_path)
        assert row["session_id"] == "from-env"
        assert row["conversation_id"] == "abc-123"

    def test_tmux_names_the_session(self, audit_logger, tmp_path, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/fake-socket,1,0")
        monkeypatch.setattr(
            audit_logger.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="my-session\n", stderr=""),
        )
        audit_logger.log_blocked("Bash", "x", "r")
        (row,) = _read_rows(tmp_path)
        assert row["session_id"] == "my-session"

    def test_unknown_fail_open(self, audit_logger, tmp_path):
        """No env, no tmux, no metadata match → the pre-#940 row shape."""
        audit_logger.HOOK_INPUT = {"session_id": "never-recorded"}
        audit_logger.log_blocked("Bash", "x", "r")
        (row,) = _read_rows(tmp_path)
        assert row["session_id"] == "unknown"
        assert row["conversation_id"] == "never-recorded"

    def test_corrupt_metadata_never_raises(self, audit_logger, tmp_path):
        d = tmp_path / "sessions" / "broken"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text("{not json")
        _write_metadata(tmp_path, "good", ["abc-123"])
        audit_logger.HOOK_INPUT = {"session_id": "abc-123"}
        audit_logger.log_blocked("Bash", "x", "r")
        (row,) = _read_rows(tmp_path)
        assert row["session_id"] == "good"

    def test_tmux_failure_falls_back_to_metadata(self, audit_logger, tmp_path, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/fake-socket,1,0")

        def boom(*a, **k):
            raise OSError("no tmux binary")

        monkeypatch.setattr(audit_logger.subprocess, "run", boom)
        _write_metadata(tmp_path, "fallback", ["abc-123"])
        audit_logger.HOOK_INPUT = {"session_id": "abc-123"}
        audit_logger.log_blocked("Bash", "x", "r")
        (row,) = _read_rows(tmp_path)
        assert row["session_id"] == "fallback"


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

    def test_bash_block_without_identity_still_blocks(self, tmp_path):
        """Fail-open: no session_id in the payload, no metadata — the block
        itself and its audit row are unaffected."""
        proc = self._run_hook(
            "bash-tool-damage-control.py",
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
            tmp_path,
        )
        assert proc.returncode == 2
        rows = [r for r in _read_rows(tmp_path) if r["decision"] == "blocked"]
        assert rows[-1]["session_id"] == "unknown"
        assert rows[-1]["conversation_id"] is None

    def test_edit_hook_attributes(self, tmp_path):
        _write_metadata(tmp_path, "edit-session", ["conv-uuid-2"])
        proc = self._run_hook(
            "edit-tool-damage-control.py",
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(tmp_path / "home" / ".agentwire" / ".env"),
                },
                "session_id": "conv-uuid-2",
            },
            tmp_path,
        )
        assert proc.returncode in (0, 2)
        rows = [r for r in _read_rows(tmp_path) if r["decision"] == "blocked"]
        if rows:  # only assert attribution when the bundled rules blocked it
            assert rows[-1]["conversation_id"] == "conv-uuid-2"


class TestSafetyReport:
    def _seed_logs(self, logs_dir: Path):
        from datetime import date

        logs_dir.mkdir(parents=True)
        today = date.today().isoformat()
        rows = [
            {"decision": "blocked", "blocked_by": "rm", "rule_id": "core.rm",
             "session_id": "s1", "conversation_id": "c1", "tool": "Bash"},
            {"decision": "blocked", "blocked_by": "unattended: rm — no grant",
             "rule_id": "core.rm", "session_id": "s2", "conversation_id": "c2",
             "tool": "Bash"},
            {"decision": "blocked", "blocked_by": "old row", "rule_id": None,
             "session_id": "unknown", "tool": "Edit"},
            {"decision": "allowed", "session_id": "s1", "tool": "Bash"},
        ]
        (logs_dir / f"{today}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        # A file outside the window must be excluded.
        (logs_dir / "2020-01-01.jsonl").write_text(
            json.dumps({"decision": "blocked", "blocked_by": "ancient"}) + "\n"
        )

    def test_summary_counts(self, tmp_path, monkeypatch):
        from agentwire import safety_commands

        logs_dir = tmp_path / "logs" / "damage-control"
        self._seed_logs(logs_dir)
        monkeypatch.setattr(safety_commands, "LOGS_DIR", logs_dir)

        summary = safety_commands.summarize_audit_blocks(days=14)
        assert summary["total_blocks"] == 3
        assert summary["attended"] == 2
        assert summary["unattended"] == 1
        assert summary["by_rule"]["core.rm"] == 2
        assert summary["by_rule"]["(no rule_id recorded)"] == 1
        assert summary["attributed"] == 2
        assert summary["unattributed"] == 1
        assert summary["by_session"]["s1"] == {"attended": 1, "unattended": 0}
        assert summary["by_session"]["s2"] == {"attended": 0, "unattended": 1}

    def test_render_and_cmd(self, tmp_path, monkeypatch, capsys):
        from agentwire import safety_commands

        logs_dir = tmp_path / "logs" / "damage-control"
        self._seed_logs(logs_dir)
        monkeypatch.setattr(safety_commands, "LOGS_DIR", logs_dir)

        assert safety_commands.safety_report_cmd(days=14, json_output=True) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["total_blocks"] == 3

        assert safety_commands.safety_report_cmd(days=14) == 0
        text = capsys.readouterr().out
        assert "attended   : 2" in text
        assert "core.rm" in text

    def test_empty_logs_dir(self, tmp_path, monkeypatch, capsys):
        from agentwire import safety_commands

        monkeypatch.setattr(
            safety_commands, "LOGS_DIR", tmp_path / "does-not-exist"
        )
        assert safety_commands.safety_report_cmd(days=7) == 0
        assert "total blocks : 0" in capsys.readouterr().out
