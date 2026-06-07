"""Tests for agentwire/council/state.py — sitting lifecycle state."""

import pytest

from agentwire.council import state


@pytest.fixture(autouse=True)
def council_dirs(tmp_path, monkeypatch):
    """Point all council paths at a temp dir."""
    council = tmp_path / "council"
    monkeypatch.setattr(state, "COUNCIL_DIR", council)
    monkeypatch.setattr(state, "SITTING_PATH", council / "sitting.json")
    monkeypatch.setattr(state, "WORKSPACE_DIR", council / "workspace")
    monkeypatch.setattr(state, "PROMPTS_DIR", council / "prompts")
    return council


def _sitting(**overrides) -> state.Sitting:
    base = dict(
        orchestrator=state.ORCHESTRATOR_SESSION,
        roster=["brain", "gut"],
        sessions={"brain": "council-brain", "gut": "council-gut"},
        started_at="2026-06-06T00:00:00+00:00",
    )
    base.update(overrides)
    return state.Sitting(**base)


class TestSitting:
    def test_round_trip(self):
        state.write_sitting(_sitting())
        loaded = state.read_sitting()
        assert loaded is not None
        assert loaded.roster == ["brain", "gut"]
        assert loaded.sessions["gut"] == "council-gut"
        assert loaded.next_prompt_id == 1
        assert loaded.session_type == "claude-bypass"

    def test_read_missing(self):
        assert state.read_sitting() is None

    def test_read_corrupt(self):
        state.SITTING_PATH.parent.mkdir(parents=True)
        state.SITTING_PATH.write_text("{not json")
        assert state.read_sitting() is None

    def test_clear(self):
        state.write_sitting(_sitting())
        state.clear_sitting()
        assert state.read_sitting() is None
        state.clear_sitting()  # idempotent


class TestPromptIds:
    def test_allocate_increments_and_persists(self):
        state.write_sitting(_sitting())
        assert state.allocate_prompt_id() == 1
        assert state.allocate_prompt_id() == 2
        assert state.read_sitting().next_prompt_id == 3

    def test_allocate_without_sitting_raises(self):
        with pytest.raises(RuntimeError):
            state.allocate_prompt_id()

    def test_latest_none_before_first_ask(self):
        state.write_sitting(_sitting())
        assert state.latest_prompt_id() is None

    def test_latest_after_allocations(self):
        state.write_sitting(_sitting())
        state.allocate_prompt_id()
        state.allocate_prompt_id()
        assert state.latest_prompt_id() == 2


class TestLensValidation:
    def test_valid(self):
        for name in ["brain", "devils-advocate", "x2"]:
            assert state.valid_lens(name)

    def test_invalid(self):
        for name in ["", "Brain", "a b", "../etc", "-lead", "a/b"]:
            assert not state.valid_lens(name)

    def test_session_for(self):
        assert state.session_for("brain") == "council-brain"
