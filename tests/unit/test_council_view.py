"""Tests for agentwire/council/view.py — the read-only board derivation."""

import pytest

from agentwire.council import inbox, state, view

ROSTER = ["brain", "gut", "critic"]
NAME = "proj"


@pytest.fixture(autouse=True)
def council_root(tmp_path, monkeypatch):
    root = tmp_path / "council"
    monkeypatch.setattr(state, "COUNCIL_ROOT", root)
    return root


@pytest.fixture
def sitting():
    s = state.Sitting(
        orchestrator=state.orchestrator_for(NAME),
        roster=ROSTER,
        sessions={lens: state.session_for(NAME, lens) for lens in ROSTER},
        started_at=state.now_iso(),
        next_prompt_id=1,
    )
    state.write_sitting(NAME, s)
    return s


@pytest.fixture
def prompt(sitting):
    pid = state.allocate_prompt_id(NAME)
    inbox.create_prompt(NAME, pid, "Should we ship X?", ROSTER)
    return pid


class TestDeriveTile:







    def test_dead_never_repaints_a_filed_take(self, prompt):
        """A terminal take must survive even if the soul's session has died."""
        inbox.write_reply(NAME, prompt, "brain", "take", "ship it")
        tile = view.derive_tile(NAME, prompt, "brain", dead=True)
        assert tile["status"] == view.STATUS_ANSWERED


class TestSnapshot:

    def test_roster_drives_tiles(self, prompt):
        snap = view.snapshot(NAME)
        assert snap["sitting"] == NAME
        assert snap["roster"] == ROSTER
        assert [t["soul"] for t in snap["tiles"]] == ROSTER
        assert snap["total"] == 3
        assert snap["prompt_id"] == prompt
        assert snap["prompt_text"] == "Should we ship X?"




