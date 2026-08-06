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

import inspect

import pytest

from agentwire import inbox, session_ready
from agentwire.voice_layer import confirm, write_tools

#: A realistic worst case: a long instruction, a long verbatim utterance, and
#: the longest session name in the wild (a worktree session name nests).
LONG_INSTRUCTION = (
    "tell the reviewer that the branch is ready and that the integration tests "
    "were run twice against a clean checkout before the pull request was opened"
)
LONG_UTTERANCE = "okay yeah go ahead and confirm tango please that's the right one thanks"


def rendered_message(body: str) -> inbox.Message:
    return inbox.Message(
        id="1700000000000000000-abc123",
        sender="buddy",
        to="agentwire-dev-voice-confirm-spine",
        kind=write_tools.WRITE_KIND,
        text=body,
        ts=1700000000000,
    )


@pytest.fixture
def worst_case_body():
    return confirm.render_body(LONG_INSTRUCTION, LONG_UTTERANCE, "a1b2c3")


#: Measured from a live Claude Code pane on 2026-08-06 with
#: ``tmux capture-pane -p``, not assumed. The glyph is ``❯`` followed by a
#: NON-BREAKING space (U+00A0), and the box is bracketed by 80-wide ``─`` rules
#: with a status footer below. A fixture that uses ``"> "`` still parses, since
#: both glyphs are in ``prompt_router._PROMPT_GLYPHS`` — but it would be a
#: fixture-shaped test of a shape the real pane never produces.
REAL_PROMPT_GLYPH = "❯\xa0"
REAL_WIDTH = 80
REAL_FOOTER = (
    "  opus  …/agentwire-dev/voice-control  voice-control\n"
    "  ⏵⏵ bypass permissions on · 3 shells · ← for agents"
)


def fake_pane(rendered: str, width: int = REAL_WIDTH) -> str:
    """A pane capture with *rendered* sitting unsubmitted in the input box.

    Mirrors what ``capture-pane`` actually produces (see the constants above):
    horizontal rules bracketing the box, the real prompt glyph, the status
    footer, and long lines WRAPPED at the pane width. The wrapping is the part a
    hand-written fixture usually omits, and it is exactly what the substring
    tests have to survive.
    """
    wrapped = [rendered[i:i + width - 2] for i in range(0, len(rendered), width - 2)]
    lines = [
        "⏺ some earlier output",
        "─" * width,
        REAL_PROMPT_GLYPH + wrapped[0],
        *["  " + chunk for chunk in wrapped[1:]],
        "─" * width,
        REAL_FOOTER,
    ]
    return "\n".join(lines)


class TestTheBodyIsOneLine:
    def test_the_rendered_line_has_no_newlines(self, worst_case_body):
        message = rendered_message(worst_case_body)
        assert "\n" not in message.render()
        assert "\r" not in message.render()

    def test_newlines_in_either_input_are_collapsed(self):
        body = confirm.render_body(
            "restart\nthe portal\r\nnow", "confirm\ntango", "a1b2c3"
        )
        assert "\n" not in body and "\r" not in body

    def test_the_chip_failure_is_real_and_a_one_line_body_avoids_it(self):
        """The mechanism, demonstrated rather than asserted from the docstring.

        A multi-line paste renders as the chip; the ``stuck`` substring test
        run against the chip finds nothing, so the heal never fires.
        """
        multiline = "restart the portal\nsaid: \"confirm tango\"\n#a1b2c3"
        chip_box = "> [Pasted text #1 +3 lines]"
        assert _stuck_matches(multiline, chip_box) is False

        one_line = confirm.render_body("restart the portal", "confirm tango", "a1b2c3")
        assert _stuck_matches(one_line, "> " + one_line) is True


def _stuck_matches(rendered: str, box_content: str) -> bool:
    """``flush_session``'s stuck test, reproduced exactly.

    Copied deliberately rather than imported: it is an inline expression inside
    ``flush_session``, and :func:`test_the_stuck_test_still_looks_like_this`
    below fails if the real one drifts from this reproduction.
    """
    return "".join(rendered.split()) in "".join(box_content.split())


class TestTheBodySurvivesTheRealPastePath:
    def test_the_stuck_test_still_looks_like_this(self):
        """Guards the reproduction above against drift in ``flush_session``."""
        source = inspect.getsource(inbox.flush_session)
        assert "stuck = [" in source
        assert '"".join(m.render().split()) in "".join(' in source

    def test_the_689_heal_finds_the_message_in_a_real_box(self, worst_case_body):
        """The property the whole one-line rule exists to preserve.

        If this fails, a swallowed Enter wedges the message forever: no heal, no
        dead-letter, no owner email.
        """
        message = rendered_message(worst_case_body)
        capture = fake_pane(message.render())
        box = session_ready.input_box(capture)
        assert box is not None, "fixture must produce a parseable box"
        assert _stuck_matches(message.render(), box) is True

    def test_the_box_shows_the_whole_message_not_just_a_window(self, worst_case_body):
        message = rendered_message(worst_case_body)
        capture = fake_pane(message.render())
        assert session_ready.text_landed(capture, message.render()) is True

    def test_the_dedup_needle_fits_inside_the_scrollback_capture(
        self, worst_case_body
    ):
        """``message_on_scrollback`` matches the FULL rendered line. A body long
        enough to scroll partly out of the capture returns False, the message
        stays pending, and it re-pastes — the orchestrator acts twice."""
        message = rendered_message(worst_case_body)
        rendered = message.render()
        # Worst case: an 80-column pane, so the line occupies ceil(len/80) rows.
        rows = -(-len(rendered) // 80)
        assert rows < session_ready.VERIFY_SCROLLBACK_LINES, (
            f"{rows} rows of a {session_ready.VERIFY_SCROLLBACK_LINES}-line capture"
        )
        # And it is genuinely found once submitted.
        scrollback = "\n".join(
            [rendered, "─" * 80, "> ", "─" * 80, "  ? for shortcuts"]
        )
        assert session_ready.message_on_scrollback(scrollback, rendered) is True

    def test_the_cap_holds_for_any_input(self):
        """The cap is what makes the two properties above structural rather
        than true-for-this-fixture."""
        body = confirm.render_body("x" * 5000, "y" * 5000, "a1b2c3")
        assert len(body) <= confirm.MAX_BODY_CHARS
        message = rendered_message(body)
        rows = -(-len(message.render()) // 80)
        assert rows < session_ready.VERIFY_SCROLLBACK_LINES

    def test_the_worst_case_rendered_line_stays_under_the_measured_boundary(self):
        """Asserted against the LIVE measurement, not against a guess.

        ``MEASURED_STUCK_LIMIT_CHARS`` came out of ``tools/voice_heal_probe.py``
        pasting into a real Claude Code pane: 520 hits the stuck test, 540 does
        not (the box starts windowing). Above that the #689 heal never fires and
        the message wedges permanently.

        The worst case is a maxed-out body plus the longest sender name in the
        wild — a worktree session, which nests.
        """
        long_sender = "agentwire-dev-voice-confirm-spine"
        body = confirm.render_body("x" * 5000, "y" * 5000, "a1b2c3")
        worst = inbox.Message(
            id="1700000000000000000-abc123",
            sender=long_sender,
            to="orchestrator",
            kind=write_tools.WRITE_KIND,
            text=body,
            ts=1700000000000,
        ).render()
        assert len(worst) <= confirm.MEASURED_STUCK_LIMIT_CHARS, (
            f"worst-case rendered line is {len(worst)} chars against a measured "
            f"boundary of {confirm.MEASURED_STUCK_LIMIT_CHARS}"
        )
        # And keep real margin: the measurement is pane-dependent, so a shorter
        # pane windows sooner than the 80x24 it was taken at.
        assert len(worst) <= confirm.MEASURED_STUCK_LIMIT_CHARS * 0.8

    def test_the_paste_path_would_not_submit_early(self, worst_case_body):
        """Bracketed paste, ``enter=False``. Asserted so a regression in the
        paste primitive shows up here rather than as a corrupted write."""
        source = inspect.getsource(session_ready.paste_no_enter)
        assert "enter=False" in source
        from agentwire import pane_manager

        send_source = inspect.getsource(pane_manager.send_to_target)
        assert "paste-buffer" in send_source and '"-p"' in send_source


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

    def test_the_transcript_side_is_stripped_too(self):
        body = confirm.render_body("restart", "confirm tango\x1b[2J", "a1b2c3")
        assert not confirm._CONTROL_RE.search(body)

    def test_the_instruction_is_stripped_at_propose_so_the_frozen_argv_is_clean(
        self, monkeypatch
    ):
        """The realistic carrier is ``instruction`` — model-supplied and only
        length-bounded. Stripping at propose keeps the frozen argv clean by
        construction, so "frozen" still means what it claims."""
        from agentwire.voice_layer import transcript
        from agentwire.voice_layer import write_tools as wt

        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})
        spine = confirm.ConfirmSpine(transcript.TranscriptRing(), wait_s=0.0)
        result = wt.propose_session_message(
            {
                "session": "orchestrator",
                "message": "restart \x1b[31mthe portal\x07",
                "_buddy": "buddy",
            },
            spine,
        )
        proposal = next(p for p in spine.pending() if p.id == result["proposal_id"])
        assert not confirm._CONTROL_RE.search(proposal.instruction)
        assert not confirm._CONTROL_RE.search(result["message"])

    def test_unicode_is_not_stripped(self):
        """Measured: curly quotes, em dashes, accents and emoji round-trip
        cleanly. Only control characters break — do not over-strip."""
        body = confirm.render_body(
            "restart the “portal” — café ✓ 🎉", "confirm tango", "a1b2c3"
        )
        for char in "“”—é✓🎉":
            assert char in body, char


class TestTheCoalescedResidualIsStated:
    """§3.7 discipline applied to the cap: narrow, do not qualify.

    The one-line rule protects the SINGLE-message case. A voice write coalesced
    behind other messages is governed by the coalesced line count — a variable
    the voice layer cannot observe — and no per-caller cap can bound it. The
    wiki must not let the cap imply a guarantee it does not have.
    """

    def test_the_wiki_states_the_coalesced_residual(self):
        from pathlib import Path

        page = (
            Path(__file__).resolve().parents[2] / "docs" / "wiki" / "voice-layer.md"
        ).read_text(encoding="utf-8")
        flat = " ".join(page.split())
        assert "protects the **single-message case**" in flat
        assert "no per-caller fix can bound it" in flat
        assert 'Do not read the cap as "one line, so the heal fires."' in flat
        # And the measured cliff is recorded, not just asserted.
        assert "Four lines chips at 87 characters" in flat
        assert "wedges every one of them" in flat


class TestTheCohortInteraction:
    """`--kind request` drags in one interaction the deferred `voice` kind
    would not have had. Named here so it is a known quantity, not a surprise.
    """

    def test_a_buddy_write_matches_the_cohort_report_filter(self, tmp_path, monkeypatch):
        """``request`` is in ``cohort.REPORT_KINDS`` and ``_harvest`` filters on
        kind — so IF the buddy were ever a pending cohort child of the
        recipient, ``wait --children`` would harvest this write as a child
        report and consume it.

        Asserted as a fact about the mechanism, because it is one. What makes
        it unreachable today is the separate property below.
        """
        from agentwire import cohort

        monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
        monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "events.jsonl")
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})

        body = confirm.render_body("restart the portal", "confirm tango", "a1b2c3")
        inbox.enqueue(
            "orchestrator", body, kind=write_tools.WRITE_KIND, sender="buddy"
        )
        harvested = cohort._harvest("orchestrator")
        assert "buddy" in harvested, (
            "kind filter admits it — the mitigation is enrollment, not kind"
        )

    def test_the_buddy_is_never_enrolled_in_a_cohort(self):
        """Why the above does not bite: nothing enrols the buddy.

        Cohort membership is written by ``agentwire new`` / ``worktree``, and
        the buddy is registered by ``identity.register``, which does not enrol.
        If that ever changes, this test fails and the interaction above becomes
        live — which is the point of asserting it.
        """
        from agentwire.voice_layer import identity

        source = inspect.getsource(identity.register)
        assert "cohort" not in source
        assert "enroll" not in source.lower()
