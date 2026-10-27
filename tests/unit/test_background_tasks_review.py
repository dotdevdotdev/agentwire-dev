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




# A. LOST COMPLETION: a quoted footer in scrollback (e.g. the agent cat'd a
# captured pane / fixture / this very PR's test) outvotes a finished turn.
def test_a1_quoted_footer_in_scrollback_does_not_make_finished_session_pending():
    quoted = (FIX / "pane_80_shell.txt").read_text()
    text = quoted + "\n⏺ done.\n✻ Brewed for 1s · done 7:41 PM\n" + BOX
    assert bt.pane_background_count(text) == 0


# A. older turn-end "still running" + interrupted/no-turn-end turn after it


# B. PREMATURE KILL: unknown task-kind wording yields 0 (=> reap), not unknown
@pytest.mark.parametrize("kind", ["1 local agent", "1 hook"])
def test_b1_unparsed_still_running_must_not_read_as_zero(kind):
    text = f"✻ Worked for 3s · done 7:37 PM · {kind} still running\n" + BOX
    assert bt.pane_background_count(text) != 0  # None (unknown) or >0 both acceptable


# B. line wrapped INSIDE the duration at narrow widths -> turn-end regex misses


# B. no turn-end line at all (showTurnDuration off / non-turn wake) and footer truncated


# B. qualify (30min) for a WORKER PANE must not fall through to the reap


# A/C. tick marks qualified for pane!=0 WITHOUT sending => hook defers forever


# C. tick sets qualified=True BEFORE enqueue; failed enqueue loses the notice for good


# C. same-name session reborn inherits the dead one's clock
