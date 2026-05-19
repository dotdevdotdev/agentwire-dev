"""Tests for ``agentwire.missions.state`` — local state JSON files."""

import json

import pytest

from agentwire.missions import state


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Redirect state paths at a tmp dir so tests don't touch real ``~/.agentwire/``."""
    state_dir = tmp_path / "missions-state"
    monkeypatch.setattr(state, "STATE_DIR", state_dir)
    monkeypatch.setattr(state, "LAST_TICK_PATH", state_dir / "last_tick.json")
    monkeypatch.setattr(state, "ROUTED_REVIEWS_PATH", state_dir / "routed_reviews.json")
    return state_dir


class TestTick:
    def test_record_creates_dir_and_file(self, isolated_state_dir):
        state.record_tick("dispatcher")
        assert isolated_state_dir.exists()
        assert (isolated_state_dir / "last_tick.json").exists()
        data = state.read_last_tick()
        assert "dispatcher" in data

    def test_record_does_not_overwrite_other_components(self):
        state.record_tick("dispatcher")
        state.record_tick("gc")
        data = state.read_last_tick()
        assert {"dispatcher", "gc"} <= set(data.keys())

    def test_read_missing_returns_empty(self):
        assert state.read_last_tick() == {}


class TestRoutedReviews:
    def test_initial_empty(self):
        assert state.read_routed_reviews() == {}

    def test_update_and_read(self):
        state.update_routed_review(42, 1001)
        assert state.last_routed_review(42) == 1001

    def test_update_overwrites(self):
        state.update_routed_review(42, 1001)
        state.update_routed_review(42, 1042)
        assert state.last_routed_review(42) == 1042

    def test_multiple_prs(self):
        state.update_routed_review(42, 1001)
        state.update_routed_review(43, 2001)
        assert state.last_routed_review(42) == 1001
        assert state.last_routed_review(43) == 2001

    def test_last_routed_for_unknown_pr(self):
        state.update_routed_review(42, 1001)
        assert state.last_routed_review(99) is None

    def test_forget_pr(self):
        state.update_routed_review(42, 1001)
        state.update_routed_review(43, 2001)
        state.forget_pr(42)
        assert state.last_routed_review(42) is None
        assert state.last_routed_review(43) == 2001

    def test_atomicity_leaves_no_temp_files(self, isolated_state_dir):
        state.update_routed_review(42, 1001)
        leftover = list(isolated_state_dir.glob("routed_reviews.json.*"))
        assert leftover == []

    def test_corrupt_json_falls_back_to_empty(self, isolated_state_dir):
        isolated_state_dir.mkdir(parents=True, exist_ok=True)
        (isolated_state_dir / "routed_reviews.json").write_text("{ not json")
        assert state.read_routed_reviews() == {}


def test_routed_reviews_file_is_pretty_printed(isolated_state_dir):
    state.update_routed_review(42, 1001)
    text = (isolated_state_dir / "routed_reviews.json").read_text()
    assert "\n" in text
    assert json.loads(text) == {"42": 1001}
