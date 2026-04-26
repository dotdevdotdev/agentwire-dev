"""Tests for `wrap_with_continuation` — the bullet-aware wrap helper.

Long lines wrap to the widget's content width; continuation chunks get
a `|` marker that matches their parent bullet's indent so the visual
connection survives the wrap.
"""

from __future__ import annotations

import pytest

from agentwire.sdk.sinks.textual import wrap_with_continuation


class TestWrapTopLevelBullet:
    def test_short_line_returns_as_is(self):
        out = wrap_with_continuation("- short", width=80)
        assert out == ["- short"]

    def test_wraps_top_level_bullet_with_pipe_continuation(self):
        text = "- thinking: hello world this is a long line"
        out = wrap_with_continuation(text, width=20)
        assert out[0].startswith("- ")
        for chunk in out[1:]:
            assert chunk.startswith("| ")
        # Concatenated chunks (minus prefixes + spaces lost on break) should
        # cover the whole input.
        joined = " ".join(c.removeprefix("- ").removeprefix("| ") for c in out)
        assert "thinking" in joined and "long line" in joined

    def test_wrap_breaks_on_space_when_possible(self):
        text = "- Bash echo alpha beta gamma delta epsilon"
        out = wrap_with_continuation(text, width=20)
        for chunk in out:
            # No mid-word breaks: each chunk's body is whitespace-clean
            body = chunk.removeprefix("- ").removeprefix("| ")
            assert not body.endswith(" ")
            assert not body.startswith(" ")


class TestWrapIndentedChild:
    def test_wraps_indented_child_with_double_pipe_continuation(self):
        text = "  · result: file written to /Users/dotdev/projects/long/path"
        out = wrap_with_continuation(text, width=24)
        assert out[0].startswith("  · ")
        for chunk in out[1:]:
            assert chunk.startswith("  | ")


class TestWrapPlainText:
    def test_plain_text_wraps_with_hang(self):
        text = "assistant text response that exceeds the column width"
        out = wrap_with_continuation(text, width=20)
        assert out[0].startswith("assistant")
        for chunk in out[1:]:
            assert chunk.startswith("  ")
            # Plain text continuations don't have a pipe — just a hang.
            assert not chunk.startswith("| ")
            assert not chunk.startswith("  | ")


class TestWrapAnsi:
    def test_preserves_ansi_open_close_per_chunk(self):
        text = "\x1b[2m- thinking: hello world this is dim text\x1b[0m"
        out = wrap_with_continuation(text, width=20)
        for chunk in out:
            assert chunk.startswith("\x1b[2m")
            assert chunk.endswith("\x1b[0m")

    def test_strips_correctly_when_no_ansi(self):
        text = "- plain bullet that wraps because it is long"
        out = wrap_with_continuation(text, width=20)
        for chunk in out:
            assert "\x1b[" not in chunk


class TestWrapEdges:
    def test_zero_width_returns_unchanged(self):
        out = wrap_with_continuation("- some line", width=0)
        assert out == ["- some line"]

    def test_too_small_width_returns_unchanged(self):
        out = wrap_with_continuation("- some line", width=3)
        assert out == ["- some line"]

    def test_long_unbreakable_word_hard_cuts(self):
        # No spaces — wrap has to hard-cut.
        text = "- " + "a" * 100
        out = wrap_with_continuation(text, width=20)
        assert len(out) > 1
        # No spaces in any chunk's body.
        for chunk in out:
            body = chunk.removeprefix("- ").removeprefix("| ")
            assert " " not in body
