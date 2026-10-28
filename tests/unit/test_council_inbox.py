"""Tests for agentwire/council/inbox.py — the fan-out/collect protocol."""

import pytest

from agentwire.council import inbox, state

ROSTER = ["brain", "gut", "critic"]
NAME = "proj"


@pytest.fixture(autouse=True)
def council_root(tmp_path, monkeypatch):
    root = tmp_path / "council"
    monkeypatch.setattr(state, "COUNCIL_ROOT", root)
    return root


@pytest.fixture
def prompt():
    inbox.create_prompt(NAME, 1, "Should we ship X?", ROSTER)
    return 1


class TestCreatePrompt:
    def test_reused_id_clears_stale_replies(self, prompt):
        """Prompt ids restart per sitting; a reused id must not inherit the
        previous sitting's reply files."""
        inbox.write_reply(NAME, 1, "brain", "pass", "")
        inbox.create_prompt(NAME, 1, "New sitting, same id", ROSTER)
        assert inbox.list_replies(NAME, 1) == []
        assert inbox.pending_souls(NAME, 1, ROSTER) == ROSTER


class TestCollect:
    def test_returns_early_when_complete(self, prompt, monkeypatch):
        for soul in ROSTER:
            inbox.write_reply(NAME, 1, soul, "pass", "")

        def boom(_):
            raise AssertionError("collect slept despite round being complete")

        monkeypatch.setattr(inbox.time, "sleep", boom)
        result = inbox.collect(NAME, 1, ROSTER, timeout=120)
        assert result["complete"]
        assert not result["timed_out"]
        assert result["pending"] == []


