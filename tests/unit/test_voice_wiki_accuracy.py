"""The wiki page is a testable artifact, and #981 is why.

`docs/wiki/voice-layer.md` accumulated sentences that were correct when written
and were falsified by later code — including two that made **opposite security
claims** in one repo. The lesson the sweep produced is not "re-read the wiki
sometimes": it is that a load-bearing sentence needs a pin, and a pin has to be
DERIVED from the code wherever it can be, so the next behaviour change fails a
test rather than quietly re-opening the gap.

So most assertions here read something out of `agentwire/voice_layer/` and look
for it in the page. The few that are literal strings are the ones where the
defect was the SHAPE of a claim — a promise of immediacy, a guarantee stated one
token wide — and there is nothing in the code to derive that from.

Every test in this module was watched RED against the pre-#981 wording. A pin
that passes against the sentence it was written to forbid is not a pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentwire import inbox
from agentwire.voice_layer import confirm, server, tools, write_tools

WIKI = Path(__file__).resolve().parents[2] / "docs" / "wiki" / "voice-layer.md"


def _flat(text: str) -> str:
    return " ".join(text.split())


#: Words that mark a claim as being QUOTED to correct it rather than asserted.
#: The page legitimately repeats a sentence it is retiring — that is how the
#: next reader learns not to restore it — so an absence test has to be
#: context-aware or it forbids the correction along with the defect. Same
#: technique as `test_nothing_rounds_the_guarantee_up`.
_CORRECTION_MARKERS = (
    "wrong", "was false", "used to", "no longer", "for one round", "not ",
    "never", "old", "stale", "instead of",
)


def _every_occurrence_is_a_correction(page: str, claim: str) -> bool:
    start = 0
    while (index := page.lower().find(claim.lower(), start)) != -1:
        context = page.lower()[max(0, index - 220) : index]
        if not any(marker in context for marker in _CORRECTION_MARKERS):
            return False
        start = index + len(claim)
    return True


def _table_row(page: str, cell: str) -> str:
    """The one markdown row whose first cell is *cell*."""
    start = page.index(f"| {cell} |")
    return page[start : page.index("|", start + len(cell) + 4) + 120]


@pytest.fixture(scope="module")
def page() -> str:
    return _flat(WIKI.read_text(encoding="utf-8"))


# =============================================================================
# The highest-value one: the page and server.py used to contradict each other
# =============================================================================


class TestWhatActuallyGuardsTheBridge:
    """Two opposite security claims in one repo (#981, wave-1 item 3).

    The page said `serve` binds loopback and that a network-reachable tool
    endpoint is "precisely the unguarded surface this design exists to avoid" —
    presenting the BIND as the guard. `server.py`'s module docstring says that
    reasoning is wrong by name, because the attacker never sends the packet: the
    owner's browser does, after a DNS rebind. The code is right.
    """

    def test_the_page_says_the_bind_is_not_the_guard(self, page):
        assert "The loopback bind is NOT the guard. The Host allowlist is." in page

    def test_the_page_explains_the_rebinding_threat_the_allowlist_answers(self, page):
        # The mechanism, not just the conclusion — a conclusion with no
        # mechanism is what gets "simplified" back to the bind next time.
        assert "the attacker never sends the packet" in page
        assert "rebinds its own name to `127.0.0.1`" in page

    def test_the_page_and_the_module_agree(self, page):
        """The defect was DISAGREEMENT, so the pin is on agreement."""
        doc = _flat(server.__doc__ or "") + _flat(server.allowed_hosts.__doc__ or "")
        for claim in (
            "the attacker never sends the packet",
            "the owner",  # "the owner's browser does" / "the OWNER'S BROWSER is"
        ):
            assert claim in doc.lower(), claim
            assert claim in page.lower(), claim

    def test_the_page_states_the_multi_user_residual(self, page):
        """`allowed_hosts` names one thing it does not close. Pages that keep
        the guarantee and drop the caveat are how a guarantee gets rounded up.
        """
        assert "loopback is per-host, not per-user" in page

    def test_the_page_does_not_claim_the_parked_thread_class_is_closed(self, page):
        # The Content-Length clamp closes the negative case only, and says so.
        assert "parked-thread class itself is NOT closed" in page


# =============================================================================
# Derived from the code
# =============================================================================


class TestTheOutcomeTableIsComplete:
    """`REASONS` is the SSOT for the taxonomy and the table encodes it.

    The table shipped 8 of 12 and the page introduced it as "never collapsed",
    which is the over-claim shape this file exists to catch.
    """

    def test_every_reason_appears(self, page):
        missing = [r for r in sorted(confirm.REASONS) if f"`{r}`" not in page]
        assert missing == []

    def test_every_wait_outcome_is_marked_as_a_wait(self, page):
        """The one property the table exists to keep straight: `refused` and
        `pending_transcript` demand OPPOSITE moves.
        """
        for reason in sorted(confirm.WAIT_OUTCOMES):
            row_start = page.index(f"| `{reason}` |")
            row = page[row_start : page.index("|", row_start + len(reason) + 8) + 200]
            assert "wait" in row.lower(), reason

    def test_the_denied_row_does_not_say_the_owner_said_no(self, page):
        """`denied` covers "wait"/"hold on" too, so asserting the owner said
        "no" is a reason that misinforms — the exact defect the taxonomy is for.
        """
        # The ROW, not the page: the prose below the table legitimately quotes
        # the retired wording in order to retire it.
        assert "you said no" not in _table_row(page, "`denied`")
        assert _every_occurrence_is_a_correction(page, "nothing — you said no")
        # And the page quotes what the code actually says.
        assert "I heard you hold off" in page
        assert "I heard you hold off" in confirm.SPOKEN["denied"]


class TestTheToolSurfaceIsNotEnumeratedStale:
    """The page listed 9 tools against a live surface many times that size.

    The repair is not a longer list — it is counts derived from the code plus a
    pointer at the tier audit, so the next tool cannot make this stale.
    """

    def test_the_read_count_matches_the_code(self, page):
        assert f"{len(tools.READ_ONLY_TOOLS)} read tools" in page

    def test_the_generated_write_count_matches_the_code(self, page):
        # One spec generates propose_/send_/cancel_, so the model sees three.
        assert len(write_tools.WRITE_TOOL_SPECS) == 3 * len(write_tools.WRITE_SPECS)
        total = len(tools.READ_ONLY_TOOLS) + len(write_tools.WRITE_TOOL_SPECS)
        assert f"{total} names" in page

    def test_the_page_points_at_the_tier_audit_as_the_ruling_document(self, page):
        assert "surface.py" in page
        for tier in ("TIER_READ", "TIER_WRITE_LIGHT", "TIER_WRITE_GATED", "TIER_EXCLUDED"):
            assert tier in page, tier

    def test_the_page_records_that_at_is_gated_by_liveness_not_by_the_character(self, page):
        """The first attempt at the remote ruling refused any `@`, which was
        itself a false statement about a creatable LOCAL session name.
        """
        assert "the gate is LIVENESS, not the `@` character" in page
        assert "local **by demonstration**" in page


class TestTheBridgeRoutesAreDocumented:
    """`/utterance` and `/anchor` are the confirm gate's ordering, and the
    architecture diagram showed neither."""

    @pytest.mark.parametrize("route", ["/mint", "/tool", "/utterance", "/anchor"])
    def test_route_appears(self, page, route):
        assert route in page


class TestTheBodyCapIsStatedNotMeasured:
    """"max rendered body 279" was true before the reply nudge existed and the
    nudge now fills toward the cap. State the cap and derive the line."""

    def test_the_stale_measurement_is_gone(self, page):
        assert "max rendered body 279" not in page

    def test_the_worst_case_line_length_is_the_derived_one(self, page):
        """Derived here the same way the page derives it, so a change to
        `MAX_BODY_CHARS` or to `Message.render`'s format fails this.
        """
        sender = "a" * 32
        message = inbox.Message(
            id="1754800000000-abcdef",
            to="target",
            sender=sender,
            kind=write_tools.WRITE_KIND,
            text="x" * confirm.MAX_BODY_CHARS,
            ts=0,
        )
        worst = len(message.render())
        assert f"{worst} against a measured {confirm.MEASURED_STUCK_LIMIT_CHARS}" in page

    def test_a_maxed_body_really_can_reach_the_cap(self):
        """The premise of the repair: the nudge fills toward MAX_BODY_CHARS, so
        the old 279 is not the maximum any more."""
        widest = max(
            len(confirm.render_body("i" * i, "u" * 90, "a1b2c3", reply_to="buddy"))
            for i in range(100, 175)
        )
        assert widest == confirm.MAX_BODY_CHARS


# =============================================================================
# Shapes of claims — nothing in the code to derive these from
# =============================================================================


class TestTheExceptionSpanIsStatedAtItsRealWidth:
    """The page said an exception suppresses "exactly one token".

    That literally describes the `len(trio or pair)` bug #987 fixed, and the
    page framed the sentence as "the form to argue a new exception in" — so the
    next contributor would argue from a guarantee NARROWER than the code
    enforces, which is the dangerous direction for a rule that suppresses
    denials.
    """

    def test_the_span_is_the_matched_rule(self, page):
        assert "exactly the tokens of its own span" in page

    def test_the_one_token_claim_is_never_asserted(self, page):
        """It may be QUOTED to retire it — that is how a deleted sentence stays
        deleted — but it may never stand as the rule."""
        assert _every_occurrence_is_a_correction(page, "exactly one token")

    def test_the_page_records_the_bug_the_claim_hid(self, page):
        assert "len(trio or pair)" in page

    def test_both_shipped_exceptions_are_documented(self, page):
        # Derived: every exception in the grammar must appear on the page.
        for pair in confirm._DENIAL_EXCEPTIONS:
            assert f'`("{pair[0]}", "{pair[1]}")`' in page, pair

    def test_the_cant_wait_false_accept_is_priced(self, page):
        """An accepted false accept that is not written down reads as an
        oversight the next reader will "fix"."""
        assert "can't — wait!" in page


class TestTheDenialGrammarMatchesTheCode:
    def test_the_gapped_never_confirm_pair_is_documented(self, page):
        assert confirm._GAPPED_DENIAL_BIGRAMS == {("never", "confirm"): confirm._NEVER_GAP_WORDS}
        assert "`(\"never\", \"confirm\")`" in page

    def test_the_page_says_the_gap_set_is_fail_open(self, page):
        """Unlike the filler set, this enumeration cannot be moved to the safe
        side, and the page has to say so rather than imply symmetry."""
        assert "sits on the fail-open side and cannot be moved off it" in page

    def test_wait_is_no_longer_described_as_unconditional(self, page):
        assert "`wait` denies unconditionally" not in page

    def test_the_measured_false_reject_is_the_live_behaviour(self):
        """The page's claim about `("cant","wait")` is that it fixed a measured
        false reject. Drive the real pipeline, not the table."""
        assert (
            confirm.classify("confirm tango, tell them I cant wait to see it", "tango")
            == confirm.APPROVED
        )
        assert confirm.classify("never ever confirm tango", "tango") == confirm.DENIED


class TestTheAttributionExampleDoesNotShipTheNonce:
    """The example body carried `said: "confirm tango"` — documenting the exact
    hole #953 closed, while the page argued two sections earlier that the nonce
    must be structurally unreachable from any echo-able channel."""

    def test_no_nonce_word_appears_in_a_said_slot(self, page):
        for word in confirm.NONCE_WORDS:
            assert f'said: "confirm {word}"' not in page, word

    def test_the_page_says_the_slot_carries_the_request_utterance(self, page):
        assert "carries the REQUEST utterance, never the approving one" in page

    def test_the_reply_nudge_slot_is_documented_as_droppable(self, page):
        assert "reply: agentwire msg send" in page
        assert "droppable, whole-or-not-at-all" in page


class TestTheInterruptTierIsNotOverPromised:
    """#993 falsified two sentences at once: the confirm-handshake window was
    described at the wrong boundary, and escalation was described as
    pre-emption it does not have."""

    def test_the_window_opens_before_the_anchor(self, page):
        assert "anchorPending" in page
        assert "holds from the proposal's anchor" not in page

    def test_preemption_is_not_claimed_unqualified(self, page):
        assert "adding no speaking path" not in page
        assert "pre-emption is real only against a **VAD**" in page

    def test_the_real_worst_case_delay_is_stated(self, page):
        """"Roughly 30s" is the difference between a promise and a description,
        and escalation is the tier a promise would be designed against."""
        assert "roughly 30s in the worst case" in page

    def test_the_page_matches_the_shipped_gate(self):
        """Derived: the page's claim is about `canInterrupt`'s legs, so read
        them out of the page source the browser actually gets."""
        from agentwire.voice_layer import client

        source = client.page("buddy", "token")
        assert "!announcer.anchorPending()" in source
        assert "!confirmGate.outstanding()" in source


class TestTheOpenResidualsAreNamed:
    """A wiki that describes what we meant is how the next contributor argues
    from a mechanism that does not exist."""

    @pytest.mark.parametrize("issue", [989, 990, 992, 995, 996, 997])
    def test_residual_is_recorded(self, page, issue):
        assert f"#{issue}" in page

    def test_the_never_completing_utterance_loop_is_described(self, page):
        assert "no staleness bound" in page

    def test_the_echoed_denial_hole_is_described(self, page):
        assert "`carries_denial` is not nonce-gated" in page


class TestTheDeliverySeamNoLongerClaimsItNeverInterrupts:
    """True of the seam, false of the layer, once #962/#967 landed."""

    def test_the_page_records_the_clock(self, page):
        assert "polls the spool every 5s" in page

    def test_the_module_docstring_agrees(self):
        from agentwire.voice_layer import delivery

        doc = _flat(delivery.__doc__ or "")
        assert "stopped being true of the layer" in doc
