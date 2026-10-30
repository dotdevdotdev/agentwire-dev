"""The voice assistant's system digest (#1198, slice 4): live busy / idle /
blocked per session, read from the pane's live status row; the shared roster;
the scheduler and config sections; injection at session start."""

import io
import json
import subprocess
import sys
import types
from datetime import datetime, timezone

import pytest

from agentwire import pane_cli, session_state, voice_digest
from agentwire import voice_hooks as vh
from agentwire.voice_layer import digest

# Pane bottoms as they rendered live (Claude Code 2.1.314).
BUSY = """⏺ Running 1 shell command…
· Discombobulating… (31s · ↓ 2.8k tokens · thought for 9s)
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  opus  …/agentwire-dev/voice-assistant-1198  voice-digest-1198
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""
BUSY_WITH_HINT = """✽ Sketching… (4s · ↓ 18 tokens)
                 ✔ Update installed · /restart to apply
─────────────────────────────────────────────────────────
❯
─────────────────────────────────────────────────────────
  sonnet  ~/projects/agentwire-dev  main
"""
OLD_BUILD_BUSY = """✻ Thinking… (esc to interrupt)
─────────────────────────────────────────────────────────
❯
─────────────────────────────────────────────────────────
"""
IDLE = """· Brewing… (12s · ↓ 300 tokens)
⏺ Four sessions are running.
✻ Cooked for 6s · done 8:08 AM
─────────────────────────────────────────────────────────
❯
─────────────────────────────────────────────────────────
  haiku  …/agentwire-dev  main
"""
BLOCKED = """ Bash command
 eval "echo approval-probe"
 │ Hook PreToolUse:Bash requires confirmation for this command:
 Do you want to proceed?
 ❯ 1. Yes
   2. No

 Esc to cancel · Tab to amend
"""


def test_live_state_is_read_from_the_live_status_row_only():
    cases = {
        "busy": [BUSY, BUSY_WITH_HINT, OLD_BUILD_BUSY],
        "idle": [IDLE],  # a spinner in SCROLLBACK above the reply is history
        "blocked": [BLOCKED],
        "unknown": ["", "dotdev@mac ~ % \n", "plain output, no input box\n"],
    }
    wrong = [(want, cap[:40], session_state.classify_capture(cap))
             for want, caps in cases.items() for cap in caps
             if session_state.classify_capture(cap) != want]
    assert not wrong, wrong


def test_an_unreadable_pane_is_unknown_never_idle(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no such pane")

    monkeypatch.setattr("agentwire.session_ready.capture_session", boom)
    assert session_state.session_state("gone") == "unknown"


def test_list_state_annotates_each_local_session(monkeypatch, capsys):
    monkeypatch.setattr(pane_cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="a:1:/tmp/a\nb:1:/tmp/b\n"))
    monkeypatch.setattr(pane_cli, "_get_session_config_from_path", lambda p: {})
    monkeypatch.setattr(pane_cli, "_display_parent", lambda n, p: None)
    monkeypatch.setattr(pane_cli, "load_session_metadata", lambda n: {})
    monkeypatch.setattr("agentwire.usage_limit.is_parked", lambda n: False)
    monkeypatch.setattr("agentwire.session_state.session_state", {"a": "busy", "b": "blocked"}.get)
    rows = pane_cli.list_local_sessions(show_state=True)
    assert [(r["name"], r["state"]) for r in rows] == [("a", "busy"), ("b", "blocked")]
    assert "state" not in pane_cli.list_local_sessions()[0]


# ── the shared roster ───────────────────────────────────────────────────

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc).timestamp()


def _fetch(sessions, projects=(), board=None, seen=None):
    def fetch(argv):
        if seen is not None:
            seen.append(argv)
        if argv[0] == "list":
            return {"success": True, "sessions": list(sessions)}
        if argv[0] == "projects":
            return {"success": True, "projects": list(projects)}
        if argv[0] == "activity":
            return {"success": True, "activity": []}
        if argv[0] == "scheduler":
            return board or {"success": False, "error": "Board file not found: x"}
        raise AssertionError(argv)
    return fetch


def test_roster_rows_carry_live_state_and_the_header_counts_it():
    seen = []
    sessions = [{"name": "a", "state": "busy"}, {"name": "b", "state": "idle"},
                {"name": "c", "state": "blocked"}, {"name": "d", "state": "unknown"},
                {"name": "svc", "service": True, "service_healthy": True}]
    roster = digest.build_roster(_fetch(sessions, seen=seen), now=NOW, overview=lambda p: "")
    assert ["list", "--sessions", "--state"] in seen
    assert "4 agents: 1 busy, 1 idle, 1 blocked;" in roster
    rows = {line.split(":")[0][2:]: line for line in roster.splitlines() if line.startswith("- ")}
    assert rows["a"].startswith("- a: busy,")
    assert rows["c"].startswith("- c: BLOCKED on a dialog,")
    assert rows["d"].startswith("- d: session")  # unknown says nothing, never "idle"


def test_truncation_hints_name_the_readers_own_tools():
    sessions = [{"name": f"s{i}", "state": "idle"} for i in range(digest.MAX_SESSION_ROWS + 3)]
    buddy = digest.build_digest(_fetch(sessions), now=NOW, overview=lambda p: "")
    assistant = voice_digest.build_assistant_digest(
        _fetch(sessions), now=NOW, overview=lambda p: "", config=_config())
    assert "+3 more (fleet_sessions)" in buddy
    assert "+3 more (sessions_list)" in assistant and "fleet_sessions" not in assistant


# ── the assistant's digest ──────────────────────────────────────────────


def _config(**over):
    cfg = types.SimpleNamespace(
        tts=types.SimpleNamespace(backend="default"), stt=types.SimpleNamespace(backend="default"),
        projects=types.SimpleNamespace(dir="/p"), services=types.SimpleNamespace(custom=[]),
        channels={"email": {"to": "x"}, "quo": {}},
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _task(name, **kw):
    return {"name": name, "enabled": True, "in_flight": False, "overdue_by": 0, "last_status": "ok", **kw}


def test_scheduler_section_surfaces_what_needs_attention():
    board = {"success": True, "tasks": [
        _task("nightly", in_flight=True),
        _task("weekly", overdue_by=7200, overdue_str="2h"),
        _task("stars", last_status="failed"),
        _task("quiet"),
        _task("off", enabled=False, in_flight=True),
    ]}
    out = voice_digest.scheduler_section(_fetch([], board=board))
    assert out.splitlines() == [
        "SCHEDULER (5 tasks, 4 enabled):",
        "- in flight: nightly",
        "- overdue: weekly (2h)",
        "- last run failed: stars",
    ]
    assert voice_digest.scheduler_section(_fetch([])) == "SCHEDULER: no board configured"


def test_scheduler_section_is_capped_across_groups():
    cap = voice_digest.MAX_SCHEDULER_ROWS
    board = {"success": True, "tasks": [
        *[_task(f"run{i}", in_flight=True) for i in range(cap - 1)],
        *[_task(f"late{i}", overdue_by=60, overdue_str="1m") for i in range(5)],
        *[_task(f"bad{i}", last_status="failed") for i in range(5)],
    ]}
    out = voice_digest.scheduler_section(_fetch([], board=board))
    named = sum(len(line.split(": ", 1)[1].split(", ")) for line in out.splitlines()[1:]
                if not line.endswith("more"))
    assert "+4 more" in out  # overdue: 1 shown of 5
    assert "last run failed" not in out  # the cap was reached before it
    assert named <= cap + 1  # names plus the one "+N more" suffix


def test_a_failing_section_says_so_and_the_rest_still_ships(monkeypatch):
    def flaky(argv):
        if argv[0] == "scheduler":
            raise TimeoutError("slow")
        return _fetch([{"name": "a", "state": "busy"}])(argv)

    out = voice_digest.build_assistant_digest(flaky, now=NOW, overview=lambda p: "",
                                              config=_config(tts=None))
    assert "- a: busy" in out
    assert "SCHEDULER: unavailable (slow)" in out
    assert out.count("CONFIG: unavailable") == 1
    assert "agentwire voice digest" in out  # the tool index still ships


def test_the_digest_stays_within_budget_for_a_huge_fleet():
    sessions = [{"name": f"session-{i:04d}-" + "x" * 60, "state": "busy"} for i in range(500)]
    projects = [{"name": f"p{i}", "path": f"/p/{i}"} for i in range(500)]
    board = {"success": True, "tasks": [_task(f"t{i}", in_flight=True) for i in range(500)]}
    out = voice_digest.build_assistant_digest(_fetch(sessions, projects, board), now=NOW,
                                              overview=lambda p: "y" * 90, config=_config())
    assert len(out) <= voice_digest.MAX_CHARS


# ── injection at session start ──────────────────────────────────────────


def test_session_start_injects_the_digest(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(vh, "_agentwire_binary", lambda: "/bin/agentwire")
    monkeypatch.setattr(vh.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw["timeout"]))
                        or types.SimpleNamespace(returncode=0, stdout="<fleet_roster>…</fleet_roster>\n"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": "startup"})))
    assert vh.main(["session-start"]) == 0
    assert capsys.readouterr().out.strip() == "<fleet_roster>…</fleet_roster>"
    assert calls == [(["/bin/agentwire", "voice", "digest"], vh.DIGEST_TIMEOUT_S)]
    assert "SessionStart" in vh.claude_settings()["hooks"]


@pytest.mark.parametrize("outcome", ["timeout", "error"])
def test_session_start_is_silent_when_the_digest_fails(monkeypatch, capsys, outcome):
    def run(cmd, **kw):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 20)
        return types.SimpleNamespace(returncode=1, stdout="Traceback …")

    monkeypatch.setattr(vh, "_agentwire_binary", lambda: "/bin/agentwire")
    monkeypatch.setattr(vh.subprocess, "run", run)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert vh.main(["session-start"]) == 0
    assert capsys.readouterr().out == ""
