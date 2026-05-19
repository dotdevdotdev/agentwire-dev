"""Tests for ``agentwire.missions.naming`` — pure derivations."""

from pathlib import Path

from agentwire.missions.naming import (
    branch_name,
    is_mission_session,
    parse_mission_session,
    session_name,
    slugify,
    worktree_path,
)


class TestSlugify:
    def test_simple(self):
        assert slugify("Add a logout button") == "add-a-logout-button"

    def test_already_kebab(self):
        assert slugify("add-thing-now") == "add-thing-now"

    def test_unicode_normalized(self):
        assert slugify("Café Olé") == "cafe-ole"

    def test_emoji_stripped(self):
        assert slugify("🚀 ship it!") == "ship-it"

    def test_punctuation_collapsed(self):
        assert slugify("Fix: bug #123, and-more!!!") == "fix-bug-123-and-more"

    def test_leading_trailing_dashes_stripped(self):
        assert slugify("---hello---") == "hello"

    def test_truncated_to_40(self):
        out = slugify("a" * 80)
        assert len(out) <= 40
        assert out == "a" * 40

    def test_truncation_strips_trailing_dash(self):
        out = slugify(("a" * 39) + "-" + ("b" * 80))
        assert len(out) <= 40
        assert not out.endswith("-")

    def test_empty_returns_untitled(self):
        assert slugify("") == "untitled"

    def test_all_symbols_returns_untitled(self):
        assert slugify("!!! @@@ ###") == "untitled"

    def test_whitespace_only_returns_untitled(self):
        assert slugify("   \t  \n  ") == "untitled"


class TestNameDerivations:
    def test_branch_name(self):
        assert branch_name(195, "foo-bar") == "mission-195-foo-bar"

    def test_session_name(self):
        assert session_name("agentwire-dev", 195, "foo-bar") == "agentwire-dev/mission-195-foo-bar"

    def test_worktree_path(self):
        out = worktree_path(Path("/projects"), "agentwire-dev", 195, "foo-bar")
        assert out == Path("/projects/agentwire-dev-worktrees/mission-195-foo-bar")


class TestParseMissionSession:
    def test_well_formed(self):
        assert parse_mission_session("agentwire-dev/mission-195-foo-bar") == (
            "agentwire-dev",
            195,
            "foo-bar",
        )

    def test_slug_with_internal_dashes(self):
        assert parse_mission_session("repo/mission-7-a-b-c") == ("repo", 7, "a-b-c")

    def test_no_slash(self):
        assert parse_mission_session("just-a-name") is None

    def test_wrong_prefix(self):
        assert parse_mission_session("agentwire-dev/something-else") is None

    def test_no_number(self):
        assert parse_mission_session("agentwire-dev/mission-foo-bar") is None

    def test_no_slug(self):
        assert parse_mission_session("agentwire-dev/mission-195-") is None

    def test_is_mission_session(self):
        assert is_mission_session("agentwire-dev/mission-195-x") is True
        assert is_mission_session("agentwire-dev/random") is False


def test_roundtrip():
    """session_name → parse_mission_session preserves all three components."""
    sn = session_name("agentwire-dev", 195, "fix-the-thing")
    assert parse_mission_session(sn) == ("agentwire-dev", 195, "fix-the-thing")
