"""The idle gate sees a running dynamic workflow (#1340).

Fixtures are REAL captures from the #1333 spike (Claude Code 2.1.319, 3-agent
workflow, paths sanitised). The old gate read every workflow-only frame as
count 0 and would have reaped the worker mid-run.
"""

from pathlib import Path

import pytest

from agentwire import background_tasks as bt

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "background_tasks"


def pane(name: str) -> str:
    return (FIX / f"pane_{name}.txt").read_text()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "aw")


def test_pane_counts_a_live_workflow_and_only_a_live_one():
    got = {name: bt.pane_background_count(pane(name)) for name in (
        "80_workflow_live_only", "80_workflow_live_with_shell",
        "80_workflow_almost_done", "80_workflow_done")}
    want = {"80_workflow_live_only": 1,  # 0/3, mode line carries no counter
            "80_workflow_live_with_shell": 3,  # 1 shell + 1 monitor + the workflow
            "80_workflow_almost_done": 1,  # 2/3 is still running
            "80_workflow_done": 0}  # 3/3 row lingers after the run
    assert got == want


def test_prose_mentioning_workflow_is_not_a_hold():
    rows = pane("80_done").splitlines()
    quoted = ["⏺ the dynamic workflow wf-spike is 1/3 done ▰▰▰▱▱▱ running in background"]
    # Quoted ABOVE the input box (history), never in the live footer position.
    idx = next(i for i, r in enumerate(rows) if r.startswith("─"))
    text = "\n".join(rows[:idx] + quoted + rows[idx:])
    assert bt.pane_background_count(text) == bt.pane_background_count(pane("80_done"))


def test_cut_off_progress_row_is_uncertain_not_zero():
    text = pane("80_workflow_live_only").replace("  0/3 · 4s · ↓ 36.2k tokens", "")
    assert bt.parse_pane(text).status == "uncertain"


def _evaluate(monkeypatch, pane_name: str, transcript: str):
    monkeypatch.setattr(bt, "_capture", lambda session, p: pane(pane_name))
    monkeypatch.setattr(bt, "find_transcript", lambda *a, **k: FIX / transcript)
    return bt.evaluate("s")


def test_workflow_in_flight_holds_and_finished_proceeds(monkeypatch):
    live = _evaluate(monkeypatch, "80_workflow_live_only", "transcript_workflow_pending.jsonl")
    done = _evaluate(monkeypatch, "80_workflow_done", "transcript_workflow_done.jsonl")
    assert (live.count, live.transcript_ids) == (1, ["w53yogq5e"])
    assert (done.count, done.transcript_ids) == (0, [])


def test_transcript_workflow_taskid_pending_until_notification():
    started = bt.pending_from_transcript(FIX / "transcript_workflow_pending.jsonl")
    ended = bt.pending_from_transcript(FIX / "transcript_workflow_done.jsonl")
    assert (started, ended) == (["w53yogq5e"], [])
