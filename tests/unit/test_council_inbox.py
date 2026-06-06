"""Tests for agentwire/council/inbox.py — the fan-out/collect protocol."""

import pytest

from agentwire.council import inbox, state

ROSTER = ["brain", "gut", "critic"]


@pytest.fixture(autouse=True)
def council_dirs(tmp_path, monkeypatch):
    council = tmp_path / "council"
    monkeypatch.setattr(state, "COUNCIL_DIR", council)
    monkeypatch.setattr(state, "SITTING_PATH", council / "sitting.json")
    monkeypatch.setattr(state, "PROMPTS_DIR", council / "prompts")
    return council


@pytest.fixture
def prompt():
    inbox.create_prompt(1, "Should we ship X?", ROSTER)
    return 1


class TestCreatePrompt:
    def test_layout(self, prompt):
        pdir = inbox.prompt_dir(1)
        assert pdir.name == "0001"
        assert (pdir / "prompt.md").read_text() == "Should we ship X?"
        assert (pdir / "replies").is_dir()
        assert inbox.read_meta(1)["roster"] == ROSTER


class TestWriteReply:
    def test_filename_per_kind(self, prompt):
        for soul, kind in [("brain", "take"), ("gut", "pass"), ("critic", "ack")]:
            path, followup = inbox.write_reply(1, soul, kind, f"{soul} text")
            assert path.name == f"{soul}.{kind}.md"
            assert not followup

    def test_invalid_kind(self, prompt):
        with pytest.raises(ValueError):
            inbox.write_reply(1, "brain", "musing", "x")

    def test_missing_inbox(self):
        with pytest.raises(FileNotFoundError):
            inbox.write_reply(99, "brain", "take", "x")

    def test_followup_numbering(self, prompt):
        inbox.write_reply(1, "brain", "ack", "")
        p1, f1 = inbox.write_reply(1, "brain", "take", "found it")
        p2, f2 = inbox.write_reply(1, "brain", "take", "more")
        assert (p1.name, f1) == ("brain.followup-1.md", True)
        assert (p2.name, f2) == ("brain.followup-2.md", True)

    def test_second_initial_rejected(self, prompt):
        inbox.write_reply(1, "brain", "take", "x")
        with pytest.raises(ValueError):
            inbox.write_reply(1, "brain", "pass", "")
        with pytest.raises(ValueError):
            inbox.write_reply(1, "brain", "ack", "")


class TestRoundCompletion:
    def test_pending_and_complete(self, prompt):
        assert inbox.pending_souls(1, ROSTER) == ROSTER
        inbox.write_reply(1, "brain", "take", "x")
        inbox.write_reply(1, "gut", "pass", "")
        assert inbox.pending_souls(1, ROSTER) == ["critic"]
        assert not inbox.initial_round_complete(1, ROSTER)
        inbox.write_reply(1, "critic", "ack", "")
        assert inbox.initial_round_complete(1, ROSTER)

    def test_mixed_kinds_count(self, prompt):
        """take, ack, and pass all complete a soul's initial round."""
        inbox.write_reply(1, "brain", "take", "x")
        inbox.write_reply(1, "gut", "ack", "")
        inbox.write_reply(1, "critic", "pass", "")
        assert inbox.initial_round_complete(1, ROSTER)


class TestListReplies:
    def test_kinds_and_followups(self, prompt):
        inbox.write_reply(1, "brain", "ack", "researching")
        inbox.write_reply(1, "gut", "pass", "")
        inbox.write_reply(1, "critic", "take", "premise is weak")
        inbox.write_reply(1, "brain", "take", "the follow-up")
        replies = inbox.list_replies(1)
        by_kind = {(r.soul, r.kind) for r in replies}
        assert by_kind == {
            ("brain", "ack"),
            ("gut", "pass"),
            ("critic", "take"),
            ("brain", "followup"),
        }
        # Follow-ups sort after initial replies
        assert replies[-1].kind == "followup"

    def test_empty(self, prompt):
        assert inbox.list_replies(1) == []

    def test_missing_prompt(self):
        assert inbox.list_replies(42) == []


class TestCollect:
    def test_returns_early_when_complete(self, prompt, monkeypatch):
        for soul in ROSTER:
            inbox.write_reply(1, soul, "pass", "")

        def boom(_):
            raise AssertionError("collect slept despite round being complete")

        monkeypatch.setattr(inbox.time, "sleep", boom)
        result = inbox.collect(1, ROSTER, timeout=120)
        assert result["complete"]
        assert not result["timed_out"]
        assert result["pending"] == []

    def test_timeout(self, prompt, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(inbox.time, "monotonic", lambda: clock["t"])

        def tick(_):
            clock["t"] += 10.0

        monkeypatch.setattr(inbox.time, "sleep", tick)
        inbox.write_reply(1, "brain", "take", "x")
        result = inbox.collect(1, ROSTER, timeout=30)
        assert not result["complete"]
        assert result["timed_out"]
        assert set(result["pending"]) == {"gut", "critic"}
        assert len(result["replies"]) == 1

    def test_no_wait_snapshots(self, prompt, monkeypatch):
        def boom(_):
            raise AssertionError("--no-wait must not sleep")

        monkeypatch.setattr(inbox.time, "sleep", boom)
        result = inbox.collect(1, ROSTER, timeout=120, wait=False)
        assert not result["complete"]
        assert not result["timed_out"]

    def test_pass_replies_carried(self, prompt):
        for soul in ROSTER:
            inbox.write_reply(1, soul, "pass", "")
        result = inbox.collect(1, ROSTER, timeout=1, wait=False)
        assert {r["kind"] for r in result["replies"]} == {"pass"}
