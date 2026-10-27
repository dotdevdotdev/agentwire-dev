"""The rendered body, measured against the REAL delivery path (branch-only).

The §4b body is the authorizing evidence for a write the owner made by voice.
Corrupting its delivery corrupts the evidence, so the constraints on it are
measured against the actual functions the drain calls — not against a model of
them.

Three properties, each with a named failure:

1. **One line.** A multi-line paste renders in Claude Code as the
   ``[Pasted text #N +M lines]`` chip and *nothing else*. ``flush_session``'s
   ``stuck`` test is a plain substring match against the box content, so it
   finds nothing, the #689 ``finish_submit`` heal never runs, ``_box_static``
   classifies it no-penalty after three sweeps, and the message is
   **permanently wedged: never healed, never dead-lettered, therefore never
   emailed** — surfacing only via ``doctor`` after two hours. For a channel
   justified by "the owner is not watching a screen", that is the worst
   available failure.
2. **Short enough that the box shows the whole thing.** The ``stuck`` test has
   no #851 window path, so a single-line body too long to render whole fails
   the heal the same way.
3. **Short enough to sit inside the dedup capture.** ``VERIFY_SCROLLBACK_LINES``
   bounds ``message_on_scrollback``; a needle that scrolls partly out returns
   False, the message stays pending, and it re-pastes — a duplicate delivery,
   which is "the orchestrator acts twice", the exact §4 failure.

The pane geometry here is real but small (80 columns), which is the
conservative direction: a narrower pane wraps more, so passing at 80 implies
passing wider.
"""


import pytest

from agentwire.voice_layer import confirm

#: A realistic worst case: a long instruction, a long verbatim utterance, and
#: the longest session name in the wild (a worktree session name nests).


#: Measured from a live Claude Code pane on 2026-08-06 with
#: ``tmux capture-pane -p``, not assumed. The glyph is ``❯`` followed by a
#: NON-BREAKING space (U+00A0), and the box is bracketed by 80-wide ``─`` rules
#: with a status footer below. A fixture that uses ``"> "`` still parses, since
#: both glyphs are in ``prompt_router._PROMPT_GLYPHS`` — but it would be a
#: fixture-shaped test of a shape the real pane never produces.


class TestTheBodyIsOneLine:

    def test_newlines_in_either_input_are_collapsed(self):
        body = confirm.render_body(
            "restart\nthe portal\r\nnow", "confirm\ntango", "a1b2c3"
        )
        assert "\n" not in body and "\r" not in body


class TestControlCharacters:
    """The known-silent wedge reached by character rewriting instead of newlines.

    A body carrying C0/C1 controls renders into the pane as an invisible control
    ACTION, so the capture no longer contains the rendered needle, ``stuck``
    misses, and the message wedges permanently. Whitespace matching does not
    cover these —
    it catches tab/newline/CR/FF/VT and nothing else.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "restart \x1b[31mthe portal\x1b[0m",   # ANSI colour
            "restart the\x07 portal",              # BEL
            "restart the\x01 portal",              # SOH
            "restart the\x7f portal",              # DEL
            "restart the\x9b portal",              # C1 CSI
        ],
    )
    def test_controls_never_reach_the_rendered_body(self, raw):
        body = confirm.render_body(raw, "confirm tango", "a1b2c3")
        assert not confirm._CONTROL_RE.search(body), repr(body)
