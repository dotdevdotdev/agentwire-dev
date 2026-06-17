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
    def test_pending(self, prompt):
        tile = view.derive_tile(NAME, prompt, "brain")
        assert tile["status"] == view.STATUS_PENDING
        assert tile["kind"] is None
        assert tile["verdict"] == ""

    def test_stalled_when_dead(self, prompt):
        tile = view.derive_tile(NAME, prompt, "brain", dead=True)
        assert tile["status"] == view.STATUS_STALLED

    def test_take(self, prompt):
        inbox.write_reply(NAME, prompt, "brain", "take", "ship it")
        tile = view.derive_tile(NAME, prompt, "brain")
        assert tile["status"] == view.STATUS_ANSWERED
        assert tile["kind"] == "take"
        assert tile["verdict"] == "ship it"
        assert tile["filed_at"]

    def test_pass(self, prompt):
        inbox.write_reply(NAME, prompt, "gut", "pass", "nothing to add")
        tile = view.derive_tile(NAME, prompt, "gut")
        assert tile["status"] == view.STATUS_PASSED
        assert tile["kind"] == "pass"

    def test_ack(self, prompt):
        inbox.write_reply(NAME, prompt, "critic", "ack", "researching…")
        tile = view.derive_tile(NAME, prompt, "critic")
        assert tile["status"] == view.STATUS_ACKED
        assert tile["kind"] == "ack"

    def test_ack_then_followup_resolves_to_take(self, prompt):
        inbox.write_reply(NAME, prompt, "critic", "ack", "researching…")
        inbox.write_reply(NAME, prompt, "critic", "take", "here is my take")
        tile = view.derive_tile(NAME, prompt, "critic")
        assert tile["status"] == view.STATUS_ANSWERED
        assert tile["kind"] == "take"
        assert tile["verdict"] == "here is my take"

    def test_highest_followup_wins(self, prompt):
        inbox.write_reply(NAME, prompt, "critic", "ack", "researching…")
        inbox.write_reply(NAME, prompt, "critic", "take", "first take")
        inbox.write_reply(NAME, prompt, "critic", "take", "second take")
        tile = view.derive_tile(NAME, prompt, "critic")
        assert tile["verdict"] == "second take"

    def test_dead_never_repaints_a_filed_take(self, prompt):
        """A terminal take must survive even if the soul's session has died."""
        inbox.write_reply(NAME, prompt, "brain", "take", "ship it")
        tile = view.derive_tile(NAME, prompt, "brain", dead=True)
        assert tile["status"] == view.STATUS_ANSWERED


class TestSnapshot:
    def test_none_without_sitting(self):
        assert view.snapshot("nope") is None

    def test_roster_drives_tiles(self, prompt):
        snap = view.snapshot(NAME)
        assert snap["sitting"] == NAME
        assert snap["roster"] == ROSTER
        assert [t["soul"] for t in snap["tiles"]] == ROSTER
        assert snap["total"] == 3
        assert snap["prompt_id"] == prompt
        assert snap["prompt_text"] == "Should we ship X?"

    def test_counter_counts_only_final_states(self, prompt):
        inbox.write_reply(NAME, prompt, "brain", "take", "yes")
        inbox.write_reply(NAME, prompt, "gut", "pass", "")
        inbox.write_reply(NAME, prompt, "critic", "ack", "later")  # NOT final
        snap = view.snapshot(NAME)
        assert snap["final"] == 2

    def test_non_default_roster(self, sitting):
        """meta.json roster — not a hardcoded 6 — drives the grid."""
        small = ["brain", "gut"]
        pid = state.allocate_prompt_id(NAME)
        inbox.create_prompt(NAME, pid, "Q?", small)
        snap = view.snapshot(NAME)
        assert snap["roster"] == small
        assert snap["total"] == 2

    def test_history_via_prompt_id(self, prompt):
        # Second round.
        pid2 = state.allocate_prompt_id(NAME)
        inbox.create_prompt(NAME, pid2, "Round two?", ROSTER)
        snap_latest = view.snapshot(NAME)
        assert snap_latest["prompt_id"] == pid2
        assert snap_latest["prompt_ids"] == [prompt, pid2]
        snap_old = view.snapshot(NAME, prompt)
        assert snap_old["prompt_id"] == prompt
        assert snap_old["prompt_text"] == "Should we ship X?"

    def test_dead_souls_marked_stalled(self, prompt):
        snap = view.snapshot(NAME, dead_souls={"brain"})
        by_soul = {t["soul"]: t for t in snap["tiles"]}
        assert by_soul["brain"]["status"] == view.STATUS_STALLED
        assert by_soul["gut"]["status"] == view.STATUS_PENDING
