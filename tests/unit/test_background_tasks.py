"""Idle with a background task pending is not done (#1103).

Fixtures are REAL captures from a Claude Code 2.1.302 session holding a pending
``sleep 100`` / Monitor (80 columns and 30 columns), not hand-written shapes.
"""

import json
import os
import shutil
import stat
import subprocess
import time
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


def wrap_rows(line: str, width: int) -> list[str]:
    """Word-wrap with a 2-space continuation indent, as the real pane does."""
    rows, cur = [], ""
    for w in line.split(" "):
        if cur and len(cur) + 1 + len(w) > width:
            rows.append(cur)
            cur = "  " + w
        else:
            cur = f"{cur} {w}" if cur else w
    rows.append(cur)
    return rows


def pane_at(line: str, width: int, footer: str = "  ⏵⏵ bypass permissions on (shift+tab to cycle)") -> str:
    return "\n".join(wrap_rows(line, width) + ["─" * width, "❯ ", "─" * width, footer[:width]])


def test_pane_count_survives_every_width_that_wraps_the_status_line():
    # Includes 20/24 columns, where the wrap lands INSIDE the duration, and a
    # footer truncated by the width: certain from the turn-end line alone.
    cases = {
        "✻ Worked for 3s · done 7:37 PM · 1 shell still running": 1,
        "✻ Cogitated for 12m 45s · done 7:37 PM · 1 shell still running": 1,
        "✻ Worked for 3s · done 7:37 PM · 1 shell, 1 monitor still running": 2,
        "✻ Worked for 3s · done 7:37 PM": 0,
    }
    bad = [(w, line) for line, want in cases.items() for w in range(16, 201)
           if bt.pane_background_count(pane_at(line, w)) != want]
    assert not bad, bad[:5]


def test_unfamiliar_task_kind_is_never_zero():
    # New wording must COUNT (or hold), never read as nothing-running: the
    # pane is authoritative, so zero here would reap a live task.
    bad = []
    for kind in ("1 local agent", "2 background agents", "1 bash", "1 hook", "3 workflows"):
        line = f"✻ Worked for 3s · done 7:37 PM · {kind} still running"
        for w in (20, 30, 80):
            if not bt.pane_background_count(pane_at(line, w)):
                bad.append((kind, w))
    assert not bad, bad
    garbled = pane_at("✻ Worked for 3s · done 7:37 PM · ⚙ stuff still running", 80)
    assert bt.parse_pane(garbled).status == "uncertain"


def test_real_shapes_for_agent_interrupt_quote_and_second_notice():
    got = {n: bt.parse_pane(pane(n)) for n in (
        "80_agent", "80_interrupted", "80_quoted_footer", "80_second_notice")}
    assert {n: (r.status, r.count) for n, r in got.items()} == {
        "80_agent": ("ok", 1), "80_interrupted": ("ok", 0),
        "80_quoted_footer": ("ok", 0), "80_second_notice": ("ok", 1)}


def test_no_turn_end_line_and_truncated_footer_is_uncertain_not_zero():
    text = "⏺ waiting\n────\n❯ \n────\n  ⏵⏵ bypass permissions on ·"
    r = bt.parse_pane(text)
    assert (r.status, r.count) == ("uncertain", None)


def test_a_stale_older_turn_line_never_outvotes_the_current_one():
    text = ("✻ Cogitated for 1s · done 7:39 PM · 1 monitor still running\n"
            "⏺ The monitor finished.\n"
            "✻ Brewed for 1s · done 7:41 PM\n"
            "────\n❯ \n────\n  ⏵⏵ bypass permissions on")
    assert bt.pane_background_count(text) == 0


def test_unreadable_pane_is_unknown_not_zero():
    assert bt.parse_pane("$ ls\nfoo\n").status == "unreadable"
    assert bt.parse_pane("").status == "unreadable"


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
    seq = [(t0 + i * 300, _pend(1), "defer") for i in range(6)] + [
        (t0 + bt.DEFER_BOUND - 120, _pend(1), "defer"),
        (t0 + bt.DEFER_BOUND, _pend(1), "qualify"),
        # A SECOND consecutive notice after the bound must not repeat the qualifier.
        (t0 + bt.DEFER_BOUND + 120, _pend(1), "defer"),
        (t0 + bt.DEFER_BOUND + 240, _pend(0), "proceed"),
    ]
    got = [bt.gate("s", 0, pending=p, now=now)["action"] for now, p, _ in seq]
    assert got == [a for _, _, a in seq]
    assert bt.read_marker("s", 0) is None  # zero pending clears the deferral


def test_the_bound_never_releases_a_reap_or_a_task_exit(isolated_config):
    t0 = 1_000_000.0
    (isolated_config / "tasks").mkdir(parents=True)
    (isolated_config / "tasks" / "sched.json").write_text("{}")
    bad = []
    for session, pane in (("worker", 1), ("sched", 0)):
        for i in range(0, 14):
            out = bt.gate(session, pane, pending=_pend(1), now=t0 + i * 300 + bt.DEFER_BOUND * (i // 7))
            if out["action"] != "defer":
                bad.append((session, pane, i, out["action"]))
    assert not bad, bad


def test_uncertain_pane_holds_like_pending(isolated_config):
    unsure = bt.Pending(None, "pane", None, None, ["pane_uncertain"], uncertain=True)
    assert bt.gate("s", 0, pending=unsure, now=1_000_000.0)["action"] == "defer"
    assert "may still be running" in bt.qualifier(None, 1900)


def test_a_reborn_session_does_not_inherit_the_old_clock(isolated_config):
    t0 = 1_000_000.0
    bt.gate("s", 0, pending=_pend(1), now=t0)
    assert bt.gate("s", 0, pending=_pend(1), now=t0 + bt.DEFER_BOUND + 5)["action"] == "defer"
    bt.gate("t", 0, "conv-a", pending=_pend(1), now=t0)
    assert bt.gate("t", 0, "conv-b", pending=_pend(1), now=t0 + 60)["action"] == "defer"
    assert bt.read_marker("t", 0)["first_deferred_at"] == t0 + 60


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


def _tick_env(monkeypatch, enqueue, parent=("boss", 0)):
    from agentwire import cohort, inbox, prompt_router, services

    monkeypatch.setattr(cohort, "session_exists", lambda s: True)
    monkeypatch.setattr(bt, "evaluate", lambda *a, **k: _pend(1))
    monkeypatch.setattr(prompt_router, "resolve_parent", lambda s, p: parent)
    monkeypatch.setattr(services, "is_service_session", lambda s: False)
    monkeypatch.setattr(inbox, "enqueue", enqueue)


def test_tick_notifies_for_a_worker_pane_and_the_pane_stays_held(isolated_config, monkeypatch):
    t0 = 1_000_000.0
    sent = []
    _tick_env(monkeypatch, lambda **kw: sent.append(kw), parent=("proj", 0))
    bt.gate("proj", 1, pending=_pend(1), now=t0)
    assert bt.tick(now=t0 + bt.DEFER_BOUND + 1)["qualified"] == ["proj"]
    assert [m["to"] for m in sent] == ["proj"]
    assert bt.gate("proj", 1, pending=_pend(1), now=t0 + bt.DEFER_BOUND + 90)["action"] == "defer"


def test_a_failed_enqueue_retries_and_no_parent_is_not_called_sent(isolated_config, monkeypatch):
    t0 = 1_000_000.0
    calls = []

    def flaky(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise OSError("disk")

    _tick_env(monkeypatch, flaky)
    bt.gate("s", 0, pending=_pend(1), now=t0)
    assert bt.tick(now=t0 + bt.DEFER_BOUND + 1)["qualified"] == []
    assert bt.read_marker("s", 0)["qualified"] is False
    assert bt.tick(now=t0 + bt.DEFER_BOUND + 61)["qualified"] == ["s"]

    _tick_env(monkeypatch, lambda **kw: calls.append(kw), parent=None)
    bt.gate("root", 0, pending=_pend(1), now=t0)
    bt.tick(now=t0 + bt.DEFER_BOUND + 1)
    marker = bt.read_marker("root", 0)
    assert (marker["qualified"], marker.get("unroutable")) == (False, True)


@pytest.mark.requires_tmux
def test_parser_reads_a_real_tmux_pane_at_every_width(tmp_path):
    """Real tmux capture-pane (via pane_manager) at 20/30/40/200 columns."""
    import subprocess
    import uuid

    from agentwire import pane_manager

    line = "✻ Worked for 3s · done 7:37 PM · 1 shell still running"
    bad = []
    for width in (20, 30, 40, 200):
        name = f"bgtest-{uuid.uuid4().hex[:8]}"
        script = tmp_path / f"p{width}.txt"
        script.write_text(pane_at(line, width) + "\n")
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "-x", str(width), "-y", "24",
                        f"cat {script}; sleep 30"], check=True)
        try:
            subprocess.run(["tmux", "wait-for", "-S", "x"], check=False)
            deadline = time.time() + 5
            got = None
            while time.time() < deadline:
                got = bt.pane_background_count(pane_manager.capture_pane(name, 0, lines=60))
                if got is not None:
                    break
                time.sleep(0.05)
            if got != 1:
                bad.append((width, got))
        finally:
            subprocess.run(["tmux", "kill-session", "-t", name], check=False)
    assert not bad, bad


def test_no_box_and_no_signal_stays_unreadable_but_any_pending_text_holds():
    bare = "$ ls\nfoo\n$ "
    dialog = "Do you want to proceed?\n ❯ 1. Yes\n   2. No\n⏵⏵ bypass permissions on · 1 shell"
    assert bt.parse_pane(bare).status == "unreadable"
    assert bt.parse_pane(dialog).status == "uncertain"


def test_unknown_noun_is_not_a_task_but_an_unparsable_segment_holds():
    box = "─" * 30 + "\n❯ \n" + "─" * 30 + "\n"
    got = {}
    for seg in ("2 MCP servers failed", "3 files changed", "1 shell", "1", "1 shell, 1", "← for agents"):
        r = bt.parse_pane("⏺ hi\n" + box + "  ⏵⏵ bypass permissions on · " + seg)
        got[seg] = (r.status, r.count)
    assert got == {"2 MCP servers failed": ("ok", 0), "3 files changed": ("ok", 0),
                   "1 shell": ("ok", 1), "1": ("uncertain", None),
                   "1 shell, 1": ("uncertain", None), "← for agents": ("ok", 0)}


def test_no_parent_is_remembered_not_retried_every_tick(isolated_config, monkeypatch):
    from agentwire import prompt_router

    t0 = 1_000_000.0
    calls = []
    _tick_env(monkeypatch, lambda **kw: None, parent=None)
    monkeypatch.setattr(prompt_router, "resolve_parent",
                        lambda s, p: calls.append((s, p)))
    bt.gate("root", 0, pending=_pend(1), now=t0)
    for i in range(3):
        bt.tick(now=t0 + bt.DEFER_BOUND + 1 + i * 60)
    assert len(calls) == 1


def test_gate_crash_holds_a_reap_path_and_only_the_parent_notice_fails_open(monkeypatch, capsys):
    import argparse

    from agentwire import notify_cli, pane_manager

    monkeypatch.setattr(bt, "gate", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    out = {}
    for pane in (0, 1):
        monkeypatch.setattr(pane_manager, "get_current_session", lambda: "s")
        monkeypatch.setattr(pane_manager, "get_current_pane_index", lambda p=pane: p)
        notify_cli.cmd_idle_gate(argparse.Namespace(conversation_id="", cwd="", json=True))
        out[pane] = json.loads(capsys.readouterr().out)["action"]
    assert out == {0: "proceed", 1: "defer"}


def test_slow_wakes_still_reach_the_bound_with_a_conversation_id(isolated_config):
    t0 = 1_000_000.0
    acts = [bt.gate("s", 0, "conv", pending=_pend(1), now=t0 + 1500 * i)["action"] for i in range(4)]
    assert acts == ["defer", "defer", "qualify", "defer"]


def test_concurrent_marker_writers_never_collide(isolated_config):
    import threading

    errors = []

    def writer(i):
        try:
            for _ in range(30):
                bt.gate("s", 1, pending=_pend(1), now=1_000_000.0 + i)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors[:2]
    assert not list(bt.state_dir().glob("*.tmp"))


def test_doctor_flags_held_notices_without_a_watchdog(isolated_config, monkeypatch, capsys):
    from agentwire import doctor_cli, limits_cli

    bt.gate("s", 0, pending=_pend(1), now=1_000_000.0)
    monkeypatch.setattr(limits_cli, "watchdog_loaded", lambda: False)
    assert doctor_cli._render_idle_deferred_section() == 1
    assert "[!!]" in capsys.readouterr().out
    monkeypatch.setattr(limits_cli, "watchdog_loaded", lambda: True)
    assert doctor_cli._render_idle_deferred_section() == 0


# --- G1: the hook with a gate that gives NO usable answer ------------------

_GATE_FAILURES = {
    "exit1": "exit 1",
    "traceback": "echo Traceback >&2; exit 1",
    "partial_nonzero": 'echo \'{"action":"proceed"\'; exit 3',
    "valid_json_nonzero": 'echo \'{"action":"proceed"}\'; exit 1',
    "empty": "true",
    "garbage": "echo not json",
    "kill9": "kill -9 $$",
    "hang": 'sleep 30; echo \'{"action":"proceed"}\'',
    "missing": None,
}


def _run_hook_cell(root: Path, gate_script, pane_index: str, scheduled: bool, timeout: str = "20"):
    """Real idle-handler.sh with a stubbed tmux + agentwire; returns the calls."""
    bindir = root / "bin"
    bindir.mkdir(parents=True)
    calls = root / "calls.log"
    calls.write_text("")
    (bindir / "tmux").write_text(
        '#!/bin/bash\ncase "$*" in *pane_index*) echo %s;; *session_name*) echo proj-w;; esac\n'
        % pane_index)
    aw = bindir / "agentwire"
    if gate_script is None:
        aw_bin = root / "no-such-binary"
    else:
        aw.write_text('#!/bin/bash\necho "$@" >> %s\nif [[ "$1" == "idle-gate" ]]; then\n%s\nfi\n'
                      % (calls, gate_script))
        aw_bin = aw
    for f in bindir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    home = root / "home"
    (home / ".agentwire" / "tasks").mkdir(parents=True)
    if scheduled:
        (home / ".agentwire" / "tasks" / "proj-w.json").write_text(json.dumps(
            {"task": "t", "summary_file": "s.md", "idle_count": 0}))
    cwd = root / "cwd"
    cwd.mkdir()
    env = {**os.environ, "HOME": str(home), "TMUX_PANE": "%1", "AGENTWIRE_BIN": str(aw_bin),
           "AGENTWIRE_GATE_TIMEOUT": timeout, "PATH": f"{bindir}:{os.environ['PATH']}"}
    subprocess.run(["bash", str(HOOK)], env=env, text=True, capture_output=True, timeout=60,
                   input=json.dumps({"notification_type": "idle_prompt", "cwd": str(cwd),
                                     "session_id": "sid-1"}))
    return calls.read_text().splitlines(), home


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_a_gate_with_no_answer_holds_every_kill_path_and_only_a_plain_pane_0_sends(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    cells = [(name, script, pane, sched) for name, script in _GATE_FAILURES.items()
             for pane, sched in (("0", False), ("1", False), ("0", True))]

    def run(cell):
        name, script, pane, sched = cell
        # Only the hang cell needs a short timeout; the rest must not trip it
        # when 27 hooks start at once.
        return cell, _run_hook_cell(tmp_path / f"{name}-{pane}-{int(sched)}", script, pane,
                                    sched, "1" if name == "hang" else "20")

    with ThreadPoolExecutor(max_workers=9) as pool:
        results = list(pool.map(run, cells))

    bad = []
    for (name, _, pane, sched), (calls, home) in results:
        killed = [c for c in calls if c.startswith(("send", "kill"))]
        notified = [c for c in calls if c.startswith("notify-parent")]
        plain = pane == "0" and not sched
        if name == "missing":
            # No binary to log calls: the hold is observable as the event the
            # hook writes before it would have reached the kill path.
            if not plain and not (home / ".agentwire" / "idle-deferred-events.jsonl").exists():
                bad.append((name, pane, sched, "no gate_unusable event"))
            continue
        if plain and (killed or len(notified) != 1):
            bad.append((name, pane, sched, "plain pane 0 must send", calls))
        if not plain and (killed or notified):
            bad.append((name, pane, sched, "kill path must hold", calls))
        if not plain and not (home / ".agentwire" / "idle-deferred-events.jsonl").exists():
            bad.append((name, pane, sched, "no gate_unusable event"))
    assert not bad, bad


# --- G3: an id-ful marker is never restarted by a gap -----------------------


def test_an_id_ful_marker_survives_a_gap_and_qualifies_exactly_once(isolated_config):
    t0 = 1_000_000.0
    gap = bt.MARKER_GRACE + 60
    acts = [bt.gate("s", 0, "conv", pending=_pend(1), now=t0 + gap * i)["action"] for i in range(4)]
    assert acts == ["defer", "qualify", "defer", "defer"]
    # An id-less call still restarts on a gap (nothing says it is the same session).
    bt.gate("u", 0, pending=_pend(1), now=t0)
    assert bt.gate("u", 0, pending=_pend(1), now=t0 + gap)["action"] == "defer"


# --- G2: a persistently failing gate is visible in doctor -------------------


def test_doctor_flags_a_persistently_failing_gate(isolated_config, capsys):
    from agentwire import doctor_cli

    now = time.time()
    for i in range(2):
        bt.log_event("gate_failed", session="w", pane=1, error="boom")
    assert doctor_cli._render_idle_deferred_section() == 0  # a blip is not a fault
    capsys.readouterr()
    bt.log_event("gate_unusable", session="w", pane=1)
    assert doctor_cli._render_idle_deferred_section() == 1
    assert "[!!] The idle gate failed 3 time(s)" in capsys.readouterr().out
    assert bt.recent_gate_failures(now=now + 2 * bt.GATE_FAILURE_WINDOW) == []


def test_gate_failure_before_the_pane_is_known_holds(monkeypatch, capsys):
    import argparse

    from agentwire import notify_cli, pane_manager

    def boom():
        raise OSError("tmux gone")

    monkeypatch.setattr(pane_manager, "get_current_session", boom)
    notify_cli.cmd_idle_gate(argparse.Namespace(conversation_id="", cwd="", json=True))
    assert json.loads(capsys.readouterr().out)["action"] == "defer"
