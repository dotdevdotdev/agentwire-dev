"""Reviewer canaries for PR #1110 (round 1). Run: uv run --extra dev pytest <this file> -q
Each test asserts the CORRECT behavior; a FAIL = a defect in the PR."""
from pathlib import Path

import pytest

from agentwire import background_tasks as bt

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "background_tasks"
BOX = "────────────\n❯ \n────────────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)"


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "aw")


def _pend(n):
    return bt.Pending(n, "pane", None, n, [])


# A. LOST COMPLETION: a quoted footer in scrollback (e.g. the agent cat'd a
# captured pane / fixture / this very PR's test) outvotes a finished turn.
def test_a1_quoted_footer_in_scrollback_does_not_make_finished_session_pending():
    quoted = (FIX / "pane_80_shell.txt").read_text()
    text = quoted + "\n⏺ done.\n✻ Brewed for 1s · done 7:41 PM\n" + BOX
    assert bt.pane_background_count(text) == 0


# A. older turn-end "still running" + interrupted/no-turn-end turn after it
def test_a2_interrupted_turn_after_old_turn_end_line():
    text = ("✻ Worked for 3s · done 7:37 PM · 1 monitor still running\n"
            "⏺ (monitor finished, I kept going)\n  ⎿  Interrupted by user\n" + BOX)
    assert bt.pane_background_count(text) == 0


# B. PREMATURE KILL: unknown task-kind wording yields 0 (=> reap), not unknown
@pytest.mark.parametrize("kind", ["1 local agent", "2 background agents", "1 bash", "1 hook"])
def test_b1_unparsed_still_running_must_not_read_as_zero(kind):
    text = f"✻ Worked for 3s · done 7:37 PM · {kind} still running\n" + BOX
    assert bt.pane_background_count(text) != 0  # None (unknown) or >0 both acceptable


# B. line wrapped INSIDE the duration at narrow widths -> turn-end regex misses
@pytest.mark.parametrize("width", [20, 24])
def test_b2_wrap_inside_duration(width):
    rows = ["✻ Cogitated for 12m", "  45s · done 7:37 PM", "  · 1 shell still", "  running"]
    text = "\n".join(rows + ["─" * width, "❯ ", "─" * width, "  ⏵⏵ bypass"])
    assert bt.pane_background_count(text) != 0


# B. no turn-end line at all (showTurnDuration off / non-turn wake) and footer truncated
def test_b3_no_turn_end_line_truncated_footer():
    text = "⏺ waiting\n────\n❯ \n────\n  ⏵⏵ bypass permissions on ·"
    assert bt.pane_background_count(text) is None  # cannot tell; must not claim 0


# B. qualify (30min) for a WORKER PANE must not fall through to the reap
def test_b4_gate_qualify_is_returned_for_worker_pane_and_hook_reaps():
    t0 = 1_000_000.0
    bt.gate("s", 1, pending=_pend(1), now=t0)
    out = bt.gate("s", 1, pending=_pend(1), now=t0 + bt.DEFER_BOUND)
    # hook treats anything != defer as proceed -> summary prompt + `kill --pane 1`
    assert out["action"] == "defer", out  # FAILS: 'qualify' => live task reaped


# A/C. tick marks qualified for pane!=0 WITHOUT sending => hook defers forever
def test_c1_tick_burns_qualified_without_notifying_for_worker_pane(monkeypatch):
    from agentwire import cohort, inbox, prompt_router, services
    t0 = 1_000_000.0
    bt.gate("s", 1, pending=_pend(1), now=t0)
    sent = []
    monkeypatch.setattr(cohort, "session_exists", lambda s: True)
    monkeypatch.setattr(bt, "evaluate", lambda *a, **k: _pend(1))
    monkeypatch.setattr(prompt_router, "resolve_parent", lambda s, p: ("boss", 0))
    monkeypatch.setattr(services, "is_service_session", lambda s: False)
    monkeypatch.setattr(inbox, "enqueue", lambda **kw: sent.append(kw))
    bt.tick(now=t0 + bt.DEFER_BOUND + 1)
    nxt = bt.gate("s", 1, pending=_pend(1), now=t0 + bt.DEFER_BOUND + 600)
    assert sent or nxt["action"] != "defer", "worker pane: bound spent, nobody told, deferred forever"


# C. tick sets qualified=True BEFORE enqueue; failed enqueue loses the notice for good
def test_c2_failed_enqueue_burns_the_one_notice(monkeypatch):
    from agentwire import cohort, inbox, prompt_router, services
    t0 = 1_000_000.0
    bt.gate("s", 0, pending=_pend(1), now=t0)
    monkeypatch.setattr(cohort, "session_exists", lambda s: True)
    monkeypatch.setattr(bt, "evaluate", lambda *a, **k: _pend(1))
    monkeypatch.setattr(prompt_router, "resolve_parent", lambda s, p: ("boss", 0))
    monkeypatch.setattr(services, "is_service_session", lambda s: False)
    def boom(**kw): raise OSError("disk")
    monkeypatch.setattr(inbox, "enqueue", boom)
    bt.tick(now=t0 + bt.DEFER_BOUND + 1)
    assert bt.read_marker("s", 0)["qualified"] is False  # retried next tick


# C. same-name session reborn inherits the dead one's clock
def test_c3_new_session_same_name_gets_fresh_bound():
    t0 = 1_000_000.0
    bt.gate("s", 0, pending=_pend(1), now=t0)         # old session, killed before any tick
    out = bt.gate("s", 0, pending=_pend(1), now=t0 + bt.DEFER_BOUND + 5)  # NEW session
    assert out["action"] == "defer"
