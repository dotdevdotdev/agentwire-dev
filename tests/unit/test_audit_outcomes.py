"""Phase 0 of #1068: record what happened AFTER damage-control decided.

Covers the three moving parts together: decision rows carry the join key,
the outcome hook writes content-free rows and can never break a tool call, and
the join turns (decision, outcome) pairs into the labels rule tuning counts.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from agentwire.hooks_cli import _managed_hook_files, get_hooks_source
from agentwire.safety.audit import join_outcomes, summarize_outcomes
from tests.conftest import HOOKS_DIR

OUTCOME_HOOK = get_hooks_source() / "outcome_logger.py"


def _run_hook(payload, tmp_path, raw=None):
    return subprocess.run(
        [sys.executable, str(OUTCOME_HOOK)],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True, text=True, timeout=10,
        env={"AGENTWIRE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )


def _rows(tmp_path):
    out = []
    for f in sorted((tmp_path / "logs" / "damage-control").glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


def test_decision_rows_carry_the_join_key(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("audit_logger_outcomes", HOOKS_DIR / "audit_logger.py")
    audit_logger = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit_logger)
    monkeypatch.setenv("AGENTWIRE_DIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    audit_logger.HOOK_INPUT = {"session_id": "conv-1", "tool_use_id": "toolu_1", "permission_mode": "auto"}
    audit_logger.log_asked("Bash", "git push", "Git: push")
    (row,) = _rows(tmp_path)
    assert (row["tool_use_id"], row["permission_mode"]) == ("toolu_1", "auto")


def test_outcome_hook_writes_a_content_free_row_and_never_fails(tmp_path):
    secret = "SECRET-COMMAND-TEXT"
    proc = _run_hook({
        "hook_event_name": "PostToolUse", "tool_use_id": "toolu_1", "tool_name": "Bash",
        "session_id": "conv-1", "permission_mode": "default", "cwd": "/repo", "duration_ms": 12,
        "tool_input": {"command": secret}, "tool_response": {"stdout": secret, "interrupted": False},
    }, tmp_path)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    (row,) = _rows(tmp_path)
    assert (row["kind"], row["result"], row["tool_use_id"], row["duration_ms"]) == ("outcome", "executed", "toolu_1", 12)
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


def test_event_results_and_denial_reason(tmp_path):
    for event in ("PostToolUseFailure", "PermissionDenied"):
        _run_hook({"hook_event_name": event, "tool_use_id": f"id-{event}", "reason": "x" * 500}, tmp_path)
    by_event = {r["event"]: r for r in _rows(tmp_path)}
    assert by_event["PostToolUseFailure"]["result"] == "failed"
    assert by_event["PermissionDenied"]["result"] == "denied"
    assert len(by_event["PermissionDenied"]["reason"]) == 200
    assert "reason" not in by_event["PostToolUseFailure"]


def test_join_labels_every_decision_outcome_pair():
    rows = [
        {"tool_use_id": "a", "decision": "allowed", "rule_id": None},
        {"tool_use_id": "b", "decision": "asked", "rule_id": "git.commit"},
        {"tool_use_id": "c", "decision": "asked", "rule_id": "git.commit"},
        {"tool_use_id": "d", "decision": "blocked", "rule_id": "core.rm"},
        {"tool_use_id": "e", "decision": "allowed", "rule_id": None},
        {"tool_use_id": "f", "decision": "allowed", "rule_id": None},
        {"decision": "blocked", "rule_id": "legacy"},
        {"kind": "outcome", "tool_use_id": "a", "result": "executed"},
        {"kind": "outcome", "tool_use_id": "b", "result": "executed"},
        {"kind": "outcome", "tool_use_id": "e", "result": "failed"},
        {"kind": "outcome", "tool_use_id": "f", "result": "denied"},
    ]
    labels = {r.get("tool_use_id", "legacy"): r["label"] for r in join_outcomes(rows)}
    assert labels == {
        "a": "ran", "b": "approved", "c": "declined-or-abandoned", "d": "rule-blocked",
        "e": "failed", "f": "classifier-denied", "legacy": None,
    }


def test_summary_reads_the_log_directory(tmp_path):
    d = tmp_path / "logs" / "damage-control"
    d.mkdir(parents=True)
    from datetime import datetime

    today = d / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    today.write_text("\n".join(json.dumps(r) for r in [
        {"tool_use_id": "b", "decision": "asked", "rule_id": "git.commit"},
        {"kind": "outcome", "tool_use_id": "b", "result": "executed"},
        {"decision": "blocked", "rule_id": "old"},
    ]) + "\n")
    s = summarize_outcomes(14, d)
    assert (s["decisions"], s["labelled"], s["unlabelled"]) == (2, 1, 1)
    assert s["by_label"] == {"approved": 1}
    assert s["by_rule"] == {"git.commit": {"approved": 1}}


def test_friction_ranking_and_daily_trend(tmp_path):
    from agentwire.safety_commands import format_safety_report

    d = tmp_path / "logs" / "damage-control"
    d.mkdir(parents=True)
    rows = []
    for i in range(3):  # a rule humans always wave through
        rows += [{"tool_use_id": f"a{i}", "timestamp": "2026-10-12T10:00:00", "decision": "asked", "rule_id": "noisy"},
                 {"kind": "outcome", "tool_use_id": f"a{i}", "result": "executed"}]
    rows += [{"tool_use_id": "d1", "timestamp": "2026-10-13T10:00:00", "decision": "asked", "rule_id": "earned"}]
    rows += [{"tool_use_id": "b1", "timestamp": "2026-10-13T10:00:00", "decision": "blocked", "rule_id": "hard"}]
    rows += [{"tool_use_id": "r1", "timestamp": "2026-10-13T10:00:00", "decision": "allowed", "rule_id": None},
             {"kind": "outcome", "tool_use_id": "r1", "result": "executed"}]
    (d / "2026-10-13.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    s = summarize_outcomes(3650, d)
    by_rule = {r["rule"]: r for r in s["friction"]}
    assert list(by_rule) == ["noisy", "earned", "hard"]
    assert by_rule["noisy"]["approve_rate"] == 1.0
    assert by_rule["earned"]["approve_rate"] == 0.0
    assert by_rule["hard"]["approve_rate"] is None and by_rule["hard"]["blocked"] == 1
    assert s["by_day"]["2026-10-12"] == {"approved": 3}
    assert s["by_day"]["2026-10-13"]["ran"] == 1
    out = format_safety_report({
        "days": 3650, "files_read": 1, "total_blocks": 0, "attended": 0, "unattended": 0,
        "attributed": 0, "unattributed": 0, "by_rule": {}, "by_tool": {}, "by_session": {}, "outcomes": s,
    })
    assert "Friction by rule" in out and "noisy" in out and "Per day" in out


def test_install_registers_the_outcome_hook_for_all_three_events():
    entry = next(e for e in _managed_hook_files() if e[0] == "outcome_logger.py")
    assert set(entry[2]) == {"PostToolUse", "PostToolUseFailure", "PermissionDenied"}
    assert Path(OUTCOME_HOOK).stat().st_mode & 0o111, "the hook must ship executable"
