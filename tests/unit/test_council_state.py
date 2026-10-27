"""Tests for agentwire/council/state.py — namespaced sitting lifecycle."""

import pytest

from agentwire.council import state

NAME = "proj"


@pytest.fixture(autouse=True)
def council_root(tmp_path, monkeypatch):
    """Point the council root at a temp dir; all per-name paths derive from it."""
    root = tmp_path / "council"
    monkeypatch.setattr(state, "COUNCIL_ROOT", root)
    return root


def _sitting(**overrides) -> state.Sitting:
    base = dict(
        orchestrator=state.orchestrator_for(NAME),
        roster=["brain", "gut"],
        sessions={
            "brain": state.session_for(NAME, "brain"),
            "gut": state.session_for(NAME, "gut"),
        },
        started_at="2026-06-06T00:00:00+00:00",
    )
    base.update(overrides)
    return state.Sitting(**base)




class TestSitting:


    def test_read_corrupt(self):
        path = state.sitting_path(NAME)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert state.read_sitting(NAME) is None


    def test_concurrent_sittings_isolated(self):
        state.write_sitting("a", _sitting(orchestrator=state.orchestrator_for("a")))
        state.write_sitting(
            "b",
            _sitting(orchestrator=state.orchestrator_for("b"), roster=["critic"]),
        )
        assert state.read_sitting("a").roster == ["brain", "gut"]
        assert state.read_sitting("b").roster == ["critic"]
        state.clear_sitting("a")
        assert state.read_sitting("a") is None
        assert state.read_sitting("b") is not None  # the other survives








