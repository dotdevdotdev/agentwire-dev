"""Reviewer canaries (G1/G2 live in test_background_tasks.py as the hook matrix and doctor test; N2/N3 are declared trade-offs). Round-3 repros for PR #1110 (assert CORRECT behavior)."""
import pytest

from agentwire import background_tasks as bt

R = "─" * 40
BOX = f"{R}\n❯ \n{R}\n"


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "aw")


# docs claim: "only an id-less marker falls back to a gap"
def test_g3_gap_restart_applies_only_to_idless_markers():
    p = bt.Pending(1, "pane", None, 1, [])
    t0 = 1_000_000.0
    bt.gate("s", 0, conversation_id="c1", pending=p, now=t0)
    out = bt.gate("s", 0, conversation_id="c1", pending=p, now=t0 + bt.DEFER_BOUND + 60)  # same conv, 31m gap, no watchdog
    assert out["action"] == "qualify", out


# L1 new noun beside "still running" holds; beside a count with NO "still running" does not count
@pytest.mark.parametrize("noun", ["2 background tasks", "1 task", "1 workflow", "1 subagent", "1 job", "3 pipelines"])
def test_n1_unknown_noun_beside_still_running_holds(noun):
    t = f"✻ Worked for 3s · done 7:37 PM · {noun} still running\n" + BOX + "  ⏵⏵ bypass permissions on (shift+tab to cycle)"
    r = bt.parse_pane(t)
    assert not (r.status == "ok" and r.count == 0), r

