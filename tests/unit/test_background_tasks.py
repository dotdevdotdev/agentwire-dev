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














# --- transcript signal -----------------------------------------------------




# --- predicate: which signal wins ------------------------------------------












# --- gate: defer / bound / second notice -----------------------------------


def _pend(n, notes=()):
    return bt.Pending(n, "pane", None, n, list(notes))
















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






# --- G3: an id-ful marker is never restarted by a gap -----------------------




# --- G2: a persistently failing gate is visible in doctor -------------------






# --- #1113: nits from the #1110 review --------------------------------------






