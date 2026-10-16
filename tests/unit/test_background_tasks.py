"""Idle with a background task pending is not done (#1103).

Fixtures are REAL captures from a Claude Code 2.1.302 session holding a pending
``sleep 100`` / Monitor (80 columns and 30 columns), not hand-written shapes.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from agentwire import background_tasks as bt

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "background_tasks"
HOOK = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "idle-handler.sh"


def pane(name: str) -> str:
    return (FIX / f"pane_{name}.txt").read_text()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "aw")
    return tmp_path / "aw"


# --- pane signal -----------------------------------------------------------


def test_pane_counts_across_real_shapes():
    got = {name: bt.pane_background_count(pane(name)) for name in (
        "80_shell", "30_shell", "80_monitor", "80_done", "80_shell_and_monitor")}
    want = {"80_shell": 1, "30_shell": 1, "80_monitor": 1, "80_done": 0,
            "80_shell_and_monitor": 2}
    assert got == want


def test_pane_count_survives_every_width_that_wraps_the_status_line():
    # The 30-column capture wraps the turn-end line and truncates the footer;
    # re-wrap the 80-column line at every width to prove it is not luck.
    line = "✻ Worked for 3s · done 7:37 PM · 1 shell still running"
    bad = []
    for width in range(24, 81):
        words, rows, cur = line.split(" "), [], ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > width:
                rows.append(cur)
                cur = "  " + w
            else:
                cur = f"{cur} {w}".strip() if cur else w
        rows.append(cur)
        text = "\n".join(rows + ["─" * width, "❯ ", "─" * width, "  ⏵⏵ bypass"])
        if bt.pane_background_count(text) != 1:
            bad.append(width)
    assert not bad, bad


def test_a_stale_older_turn_line_never_outvotes_the_current_one():
    text = ("✻ Cogitated for 1s · done 7:39 PM · 1 monitor still running\n"
            "⏺ The monitor finished.\n"
            "✻ Brewed for 1s · done 7:41 PM\n"
            "────\n❯ \n────\n  ⏵⏵ bypass permissions on")
    assert bt.pane_background_count(text) == 0


def test_unreadable_pane_is_unknown_not_zero():
    assert bt.pane_background_count("$ ls\nfoo\n") is None
    assert bt.pane_background_count("") is None


# --- transcript signal -----------------------------------------------------


def test_transcript_pending_across_real_shapes():
    got = {n: bt.pending_from_transcript(FIX / f"transcript_{n}.jsonl") for n in (
        "bash_pending", "bash_done", "monitor_events", "monitor_done")}
    # Monitor per-event notifications carry no <status> and are NOT terminal.
    assert got == {"bash_pending": ["b1ldrff5q"], "bash_done": [],
                   "monitor_events": ["bccmeth8k"], "monitor_done": []}


# --- predicate: which signal wins ------------------------------------------


def _eval(monkeypatch, tmp_path, *, pane_text, transcript):
    t = None
    if transcript:
        t = tmp_path / "t.jsonl"
        shutil.copy(FIX / f"transcript_{transcript}.jsonl", t)
    monkeypatch.setattr(bt, "find_transcript", lambda *a, **k: t)
    monkeypatch.setattr(bt, "_capture", lambda *a, **k: pane_text)
    return bt.evaluate("s", 0)


def test_pane_is_authoritative_and_stale_transcript_is_zero(monkeypatch, tmp_path):
    # A task started before an /exit + --resume looks pending forever in the
    # transcript; the pane shows what the harness believes right now.
    ev = _eval(monkeypatch, tmp_path, pane_text=pane("80_done"), transcript="bash_pending")
    assert (ev.count, ev.source, "stale_transcript" in ev.notes) == (0, "pane", True)


def test_pane_only_tasks_still_count(monkeypatch, tmp_path):
    ev = _eval(monkeypatch, tmp_path, pane_text=pane("80_shell"), transcript="bash_done")
    assert (ev.count, "pane_only" in ev.notes) == (1, True)


def test_transcript_is_the_fallback_when_pane_unreadable(monkeypatch, tmp_path):
    ev = _eval(monkeypatch, tmp_path, pane_text=None, transcript="bash_pending")
    assert (ev.count, ev.source) == (1, "transcript")


def test_nothing_readable_is_unknown(monkeypatch, tmp_path):
    ev = _eval(monkeypatch, tmp_path, pane_text=None, transcript=None)
    assert (ev.count, ev.source) == (None, "unknown")


# --- gate: defer / bound / second notice -----------------------------------


def _pend(n, notes=()):
    return bt.Pending(n, "pane", None, n, list(notes))


def test_gate_defers_then_qualifies_once_then_defers_then_clears(isolated_config):
    t0 = 1_000_000.0
    seq = [
        (t0, _pend(1), "defer"),
        (t0 + 60, _pend(1), "defer"),
        (t0 + bt.DEFER_BOUND, _pend(1), "qualify"),
        # A SECOND consecutive notice after the bound must not repeat the qualifier.
        (t0 + bt.DEFER_BOUND + 120, _pend(1), "defer"),
        (t0 + bt.DEFER_BOUND + 240, _pend(0), "proceed"),
    ]
    got = [bt.gate("s", 0, pending=p, now=now)["action"] for now, p, _ in seq]
    assert got == [a for _, _, a in seq]
    assert bt.read_marker("s", 0) is None  # zero pending clears the deferral


def test_gate_restarts_the_clock_after_tasks_finish(isolated_config):
    t0 = 1_000_000.0
    bt.gate("s", 0, pending=_pend(1), now=t0)
    bt.gate("s", 0, pending=_pend(0), now=t0 + 10)
    # New background work later must get a fresh bound, not the old clock.
    assert bt.gate("s", 0, pending=_pend(1), now=t0 + bt.DEFER_BOUND + 5)["action"] == "defer"


def test_gate_unknown_fails_open(isolated_config):
    out = bt.gate("s", 0, pending=bt.Pending(None, "unknown", notes=["pane_unreadable"]))
    assert (out["action"], out["pending"]) == ("proceed", None)


def test_qualified_text_says_not_finished():
    assert "not necessarily finished" in bt.qualifier(2, 1900)
    assert "2 background tasks" in bt.qualifier(2, 1900)


# --- watchdog sweep: the bound fires without another idle event ------------


def test_tick_sends_one_qualified_notice_past_the_bound(isolated_config, monkeypatch):
    from agentwire import cohort, inbox, prompt_router, services

    t0 = 1_000_000.0
    bt.gate("worker", 0, pending=_pend(1), now=t0)
    sent = []
    monkeypatch.setattr(cohort, "session_exists", lambda s: True)
    monkeypatch.setattr(bt, "evaluate", lambda *a, **k: _pend(1))
    monkeypatch.setattr(prompt_router, "resolve_parent", lambda s, p: ("boss", 0))
    monkeypatch.setattr(services, "is_service_session", lambda s: False)
    monkeypatch.setattr(inbox, "enqueue", lambda **kw: sent.append(kw))

    assert bt.tick(now=t0 + 60)["qualified"] == []
    assert bt.tick(now=t0 + bt.DEFER_BOUND + 1)["qualified"] == ["worker"]
    assert bt.tick(now=t0 + bt.DEFER_BOUND + 90)["qualified"] == []  # once, not per tick
    assert [(m["to"], m["kind"]) for m in sent] == [("boss", "idle")]
    assert "not necessarily finished" in sent[0]["text"]


def test_tick_drops_markers_for_finished_or_vanished_sessions(isolated_config, monkeypatch):
    from agentwire import cohort

    t0 = 1_000_000.0
    bt.gate("done", 0, pending=_pend(1), now=t0)
    bt.gate("gone", 0, pending=_pend(1), now=t0)
    monkeypatch.setattr(cohort, "session_exists", lambda s: s == "done")
    monkeypatch.setattr(bt, "evaluate", lambda *a, **k: _pend(0))
    assert sorted(bt.tick(now=t0 + 5)["cleared"]) == ["done", "gone"]
    assert list(bt.state_dir().glob("*.json")) == []


# --- the hook honors the gate ----------------------------------------------


def _run_hook(tmp_path, gate_stdout, pane_index="0"):
    """Run the real idle-handler.sh with stub tmux + agentwire binaries."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    calls = tmp_path / "calls.log"
    calls.write_text("")
    tmux = bindir / "tmux"
    tmux.write_text(
        '#!/bin/bash\ncase "$*" in *pane_index*) echo %s;; *session_name*) echo proj-w;; esac\n'
        % pane_index)
    aw = bindir / "agentwire"
    aw.write_text(
        '#!/bin/bash\necho "$@" >> %s\n'
        'if [[ "$1" == "idle-gate" ]]; then cat <<\'EOF\'\n%s\nEOF\nfi\n' % (calls, gate_stdout))
    for f in (tmux, aw):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    (home / ".agentwire").mkdir(parents=True, exist_ok=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home), "TMUX_PANE": "%1", "AGENTWIRE_BIN": str(aw),
           "PATH": f"{bindir}:{os.environ['PATH']}"}
    payload = json.dumps({"notification_type": "idle_prompt", "cwd": str(cwd),
                          "session_id": "sid-1"})
    subprocess.run(["bash", str(HOOK)], input=payload, env=env, text=True,
                   capture_output=True, timeout=30, check=True)
    return calls.read_text().splitlines()


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_hook_gate_decides_what_the_idle_does(tmp_path):
    notify = lambda calls: [c for c in calls if c.startswith("notify-parent")]  # noqa: E731
    out = {}
    out["defer"] = notify(_run_hook(tmp_path / "a", '{"action":"defer","pending":1}'))
    out["proceed"] = notify(_run_hook(tmp_path / "b", '{"action":"proceed","pending":0}'))
    out["qualify"] = notify(_run_hook(
        tmp_path / "c", '{"action":"qualify","pending":2,"text":"is idle, but 2 tasks still running"}'))
    # Gate prints garbage / nothing: behave exactly as before the gate existed.
    out["garbage"] = notify(_run_hook(tmp_path / "d", "not json"))
    out["silent"] = notify(_run_hook(tmp_path / "e", ""))
    assert out["defer"] == []
    assert out["proceed"] == out["garbage"] == out["silent"] == [
        "notify-parent --on-idle --queued -q is idle and done working"]
    assert out["qualify"] == ["notify-parent --on-idle --queued -q is idle, but 2 tasks still running"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_hook_defer_also_holds_back_the_worker_pane_reap(tmp_path):
    calls = _run_hook(tmp_path / "w", '{"action":"defer","pending":1}', pane_index="1")
    assert not [c for c in calls if c.startswith(("send", "kill"))]
