"""Live busy / idle / blocked classification (``session_state``), the only
such answer in the CLI; ``list --state`` is its consumer."""

import types

from agentwire import pane_cli, session_state

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
