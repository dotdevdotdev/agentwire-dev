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


class TestPaths:
    def test_per_name_layout(self, council_root):
        assert state.council_dir(NAME) == council_root / NAME
        assert state.sitting_path(NAME) == council_root / NAME / "sitting.json"
        assert state.workspace_dir(NAME) == council_root / NAME / "workspace"
        assert state.prompts_dir(NAME) == council_root / NAME / "prompts"

    def test_session_names(self):
        assert state.orchestrator_for(NAME) == "agentwire-council-proj"
        assert state.session_for(NAME, "brain") == "council-proj-brain"
        assert state.session_for("redesign", "devils-advocate") == (
            "council-redesign-devils-advocate"
        )

    def test_two_names_are_isolated(self):
        assert state.sitting_path("a") != state.sitting_path("b")
        assert state.session_for("a", "brain") != state.session_for("b", "brain")


class TestSitting:
    def test_round_trip(self):
        state.write_sitting(NAME, _sitting(cwd="/tmp/proj"))
        loaded = state.read_sitting(NAME)
        assert loaded is not None
        assert loaded.roster == ["brain", "gut"]
        assert loaded.sessions["gut"] == "council-proj-gut"
        assert loaded.cwd == "/tmp/proj"
        assert loaded.next_prompt_id == 1
        assert loaded.posture == "bypass"

    def test_read_missing(self):
        assert state.read_sitting(NAME) is None

    def test_read_corrupt(self):
        path = state.sitting_path(NAME)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert state.read_sitting(NAME) is None

    def test_clear(self):
        state.write_sitting(NAME, _sitting())
        state.clear_sitting(NAME)
        assert state.read_sitting(NAME) is None
        state.clear_sitting(NAME)  # idempotent

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


class TestListSittings:
    def test_empty(self):
        assert state.list_sittings() == []

    def test_lists_only_dirs_with_state(self):
        state.write_sitting("a", _sitting())
        state.write_sitting("redesign", _sitting())
        # A stray dir without sitting.json (e.g. an orphaned global workspace)
        (state.COUNCIL_ROOT / "workspace").mkdir(parents=True)
        assert set(state.list_sittings()) == {"a", "redesign"}


class TestPromptIds:
    def test_allocate_increments_and_persists(self):
        state.write_sitting(NAME, _sitting())
        assert state.allocate_prompt_id(NAME) == 1
        assert state.allocate_prompt_id(NAME) == 2
        assert state.read_sitting(NAME).next_prompt_id == 3

    def test_allocate_without_sitting_raises(self):
        with pytest.raises(RuntimeError):
            state.allocate_prompt_id(NAME)

    def test_counters_are_per_name(self):
        state.write_sitting("a", _sitting())
        state.write_sitting("b", _sitting())
        state.allocate_prompt_id("a")
        state.allocate_prompt_id("a")
        assert state.latest_prompt_id("a") == 2
        assert state.latest_prompt_id("b") is None  # untouched

    def test_latest_none_before_first_ask(self):
        state.write_sitting(NAME, _sitting())
        assert state.latest_prompt_id(NAME) is None


class TestNameValidation:
    def test_valid(self):
        for name in ["brain", "devils-advocate", "x2", "redesign", "agentwire-dev"]:
            assert state.valid_name(name)
            assert state.valid_lens(name)

    def test_invalid(self):
        for name in ["", "Brain", "a b", "../etc", "-lead", "a/b", "a.b", "a:b"]:
            assert not state.valid_name(name)


class TestDefaultName:
    def test_slugifies_dir_basename(self, tmp_path):
        d = tmp_path / "My Project"
        d.mkdir()
        # Not a git repo → git_root returns None, falls back to the dir itself.
        name = state.default_name(d)
        assert state.valid_name(name)
        assert name.startswith("my-project")

    def test_is_capped_and_hashed_when_long(self, tmp_path):
        long = tmp_path / ("x" * 60)
        long.mkdir()
        name = state.default_name(long)
        assert state.valid_name(name)
        assert len(name) <= state._NAME_MAX

    def test_deterministic(self, tmp_path):
        d = tmp_path / "repo"
        d.mkdir()
        assert state.default_name(d) == state.default_name(d)
