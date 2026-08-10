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

**The drift can flip polarity, so the pins are two-sided.** The first round of
this sweep fixed the page and left the identical false claims in the module
docstrings one file away — six of them describing the pre-#951 anchor, and one
saying `wait` denies unconditionally. A pin scoped to the page proves the page,
which is exactly the "testing a table's entries against themselves" trap
`confirm.py` records one level down. So the claims that exist on both sides are
asserted on both sides, over `SOURCES` below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentwire import inbox
from agentwire.voice_layer import confirm, server, tools, write_tools

WIKI = Path(__file__).resolve().parents[2] / "docs" / "wiki" / "voice-layer.md"
VOICE_LAYER = Path(confirm.__file__).parent

#: Every prose surface a voice-layer claim can live on. The page is one of
#: them, not the only one.
SOURCES = tuple(sorted(VOICE_LAYER.glob("*.py"))) + (WIKI,)


def _flat(text: str) -> str:
    return " ".join(text.split())


#: Phrases that mark a claim as being QUOTED to retire it rather than asserted.
#: A page legitimately repeats a sentence it is retiring — that is how the next
#: reader learns not to restore it — so an absence test has to be context-aware
#: or it forbids the correction along with the defect.
#:
#: **Deliberately UNAMBIGUOUS and deliberately not the obvious list.** The first
#: version accepted bare `not `, `never`, `wrong` and `old` inside a 220-char
#: window, which in a prose-dense module is always satisfied by something
#: unrelated: `confirm.py`'s "The problem was never enumeration as such" sat two
#: sentences above "So ``wait`` denies unconditionally" and silenced the pin
#: that existed to catch it. Six of the nine claims this module forbids went
#: green that way — the absence tests could not fail, which is the same
#: not-really-coverage defect they were written to catch one level up.
_RETIREMENT_MARKERS = (
    "used to", "no longer", "was false", "was wrong", "is wrong",
    "for one round", "previously", "before the fix", "retired", "stopped being",
    "described the old", "an earlier", "the first version",
)

#: How far back a marker may sit. One sentence, not a paragraph: a retirement
#: marker that is not in the same breath as the claim is not qualifying it.
_MARKER_WINDOW = 140


def _every_occurrence_is_a_correction(page: str, claim: str) -> bool:
    start = 0
    lowered = page.lower()
    needle = claim.lower()
    while (index := lowered.find(needle, start)) != -1:
        context = lowered[max(0, index - _MARKER_WINDOW) : index]
        if not any(marker in context for marker in _RETIREMENT_MARKERS):
            return False
        start = index + len(claim)
    return True


def _table_row(page: str, cell: str) -> str:
    """The one markdown row whose first cell is *cell*."""
    start = page.index(f"| {cell} |")
    return page[start : page.index("|", start + len(cell) + 4) + 120]


def _fenced_blocks(raw: str) -> list[str]:
    """Every ``` fenced block in the RAW page, unflattened.

    The route pins used to be page-wide substring checks, and `/utterance` and
    `/anchor` also appear in four prose paragraphs — so deleting them from the
    architecture DIAGRAM, which is the defect those pins exist to catch, left
    every one of them green. A pin has to look where the defect lives.
    """
    parts = raw.split("```")
    return parts[1::2]


def _offenders(claim: str) -> list[str]:
    """Sources asserting *claim* outside a correction context."""
    bad = []
    for path in SOURCES:
        if not _every_occurrence_is_a_correction(
            _flat(path.read_text(encoding="utf-8")), claim
        ):
            bad.append(path.name)
    return bad


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
        """The defect was DISAGREEMENT, so the pin is on agreement.

        The bare token "the owner" used to be the second claim here. It appears
        dozens of times on both sides and so could never fail — the test went
        red only on its sibling, while counting as two assertions' worth of
        coverage. An assertion that cannot fail is not coverage.
        """
        doc = (_flat(server.__doc__ or "") + _flat(server.allowed_hosts.__doc__ or "")).lower()
        for claim in (
            "the attacker never sends the packet",
            # Who DOES send it — the half that makes the first claim actionable.
            "browser",
        ):
            assert claim in doc, claim
            assert claim in page.lower(), claim
        # And the agreement is about the BIND specifically: both sides must say
        # loopback is not what keeps the remote page out.
        for text in (doc, page.lower()):
            assert "loopback bind is" in text and "not" in text

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


class TestTheResidualTableDoesNotAdvertiseClosedHoles:
    """The residual table's own header says why this matters: *"a wiki that
    describes what we meant is how the next contributor designs against
    something that does not exist"*. That cuts both ways, and the second
    direction is the one that rots silently — a CLOSED hole left listed as open
    is a mechanism the next reader believes is missing, so they either rebuild
    it or route around it. #989, #990 and #992 were the beta gate; each is now
    described where it lives instead.
    """

    CLOSED = ("#989", "#990", "#992")

    @staticmethod
    def _residual_rows() -> "list[str]":
        """The residual table's rows, from the RAW page.

        Read raw rather than through the flattened ``page`` fixture: this is a
        LINE-shaped claim, and flattening collapses the whole table onto one
        line, which is how a line-based assertion here passes (or fails) for a
        reason that has nothing to do with the table.
        """
        raw = WIKI.read_text(encoding="utf-8")
        table = raw.split("### Known residuals")[1].split("### Standing constraints")[0]
        return [line for line in table.splitlines() if line.startswith("| #")]

    @pytest.mark.parametrize("issue", CLOSED)
    def test_the_closed_ones_are_not_listed_as_residuals(self, issue):
        rows = [r for r in self._residual_rows() if r.startswith(f"| {issue} |")]
        assert rows == [], rows

    #: Words that describe a hole as still present. An OPEN marker in the same
    #: PARAGRAPH as a closed issue id is the defect this catches.
    #:
    #: **This enumeration fails open and says so**: an unlisted phrasing slips
    #: through. What bounds it is that the vocabulary is the one a residual
    #: paragraph actually uses — derived from the paragraph that survived round
    #: 1 ("the denial direction is **open** (#992)", "**Open**, and priced
    #: rather than assumed"), not invented.
    OPEN_MARKERS = (
        "is open", "are open", "open residual", "remains open", "still open",
        "open, and priced", "not fixed", "unfixed", "left uncovered",
        "pinned as behaviour", "pinned in the tests as behaviour", "to-do",
    )

    #: Words marking the paragraph as talking about the CLOSURE or the history,
    #: so an OPEN marker in that company is a correction rather than a claim.
    #: Same two-sided shape as `_RETIREMENT_MARKERS` above.
    #: Deliberately NOT the bare stem "close": this page says "closes the
    #: approval direction" in the same breath as the stale claim it is about,
    #: so a stem match let the offending paragraph neutralize itself — measured,
    #: and the same too-loose-marker defect `_RETIREMENT_MARKERS` above records.
    CLOSURE_MARKERS = (
        "closed", "was open", "used to", "no longer", "rejected",
        "round 1", "originally",
    )

    @staticmethod
    def _paragraphs() -> "list[tuple[int, str]]":
        """The RAW page as (line number, paragraph) pairs.

        **Paragraph-scoped, not window-scoped, and the difference is a measured
        false negative.** A ±400-char window around the id caught nothing when
        the stale paragraph sat immediately above the section that closes it:
        the closure sentence next door was inside the window and silenced the
        detector. The real defect was a SELF-CONTAINED paragraph making a claim,
        so the unit the pin reasons about is a paragraph.
        """
        raw = WIKI.read_text(encoding="utf-8")
        out, line = [], 1
        for block in raw.split("\n\n"):
            out.append((line, block))
            line += block.count("\n") + 2
        return out

    @pytest.mark.parametrize("issue", CLOSED)
    def test_no_PROSE_paragraph_still_calls_them_open(self, issue):
        """The pin the round-1 version structurally could not be.

        That one read residual-TABLE rows, so a paragraph 240 lines above the
        section that closes #992 could go on calling it open — and did, while
        naming the two mitigations that section rejects, 1200 lines from the
        page's own "#989/#990/#992 are CLOSED". Removing a table row is not the
        same claim as retiring a sentence, and only one of them was pinned.
        """
        offences = []
        for line, block in self._paragraphs():
            lowered = block.lower()
            if issue not in block:
                continue
            if any(m in lowered for m in self.CLOSURE_MARKERS):
                continue
            hit = [m for m in self.OPEN_MARKERS if m in lowered]
            if hit:
                offences.append((line, hit))
        assert offences == [], offences

    @pytest.mark.parametrize("issue", CLOSED)
    def test_the_prose_pin_can_actually_fire(self, issue):
        """The must-fail control for the pin above, run per issue.

        An absence test over a vocabulary is exactly the shape that goes green
        because nothing can match it — six of this file's own claims once did.
        So the detector is run over a page carrying the sentence it forbids,
        planted where the real one lived: as its own paragraph, immediately
        above the section that closes it, which is precisely the placement a
        window-scoped detector could not see.
        """
        planted = (
            f"The denial direction is open ({issue}), and nobody has taken it."
        )
        lowered = planted.lower()
        assert issue in planted
        assert any(m in lowered for m in self.OPEN_MARKERS)
        assert not any(m in lowered for m in self.CLOSURE_MARKERS), (
            "the planted sentence must not neutralize itself, or the control "
            "proves only that the closure vocabulary works"
        )

    def test_the_mechanisms_they_installed_are_documented(self, page):
        """And the other direction: removing the row without documenting the
        fix would leave the page silent about behaviour that now exists."""
        for claim in (
            "UNHEARD_COMMITTED_GRACE_S",
            "UNHEARD_OPEN_UTTERANCE_S",
            "`cancel()` takes the SAME claim",
            "The buddy's own voice cannot deny",
        ):
            assert claim in page, claim

    def test_the_residual_section_still_has_open_entries(self):
        """The must-fail control. If the table is ever empty — or the split
        above stops finding it — the parametrized test passes for the wrong
        reason."""
        assert self._residual_rows()


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
    architecture diagram showed neither.

    Anchored INSIDE the fences. As page-wide substring checks these four could
    not fail: both routes appear in four prose paragraphs elsewhere, so gutting
    the diagram — the actual defect — left all four green.
    """

    @pytest.mark.parametrize("route", ["/mint", "/tool", "/utterance", "/anchor"])
    def test_route_appears_in_the_architecture_diagram(self, route):
        raw = WIKI.read_text(encoding="utf-8")
        diagrams = [b for b in _fenced_blocks(raw) if "browser client" in b]
        assert len(diagrams) == 1, "the architecture diagram moved or split"
        assert route in diagrams[0]

    def test_every_post_route_the_bridge_serves_is_in_the_diagram(self):
        """Derived: the routes come out of `server.py`'s dispatch, so a fifth
        route cannot ship undocumented."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        served = {
            line.split('"')[1]
            for line in source.splitlines()
            if line.strip().startswith('if path == "/')
        }
        diagram = [b for b in _fenced_blocks(WIKI.read_text(encoding="utf-8"))
                   if "browser client" in b][0]
        assert served, "no routes extracted — the dispatch shape changed"
        assert {r for r in served if r not in diagram} == set()


class TestTheAnchorClaimIsCorrectedEVERYWHERE:
    """The polarity flip: round one fixed the page and left six code sites.

    `/anchor` is POSTed from `onSpoken` for BOTH values of `how`, and the
    fallback case produces no model turn at all — so "the `response.done` of the
    turn in which the buddy SPOKE it" is false for the browser-voice path. It
    survived in `client.py`, `server.py` (x2), `confirm.py` (x2) and
    `transcript.py` while the page said the right thing.
    """

    #: The exact pre-#951 formulations. Each may be QUOTED to retire it.
    RETIRED = (
        "of the turn in which the buddy SPOKE",
        "``response.done`` in which it was SPOKEN",
        "supplied by the client's ``response.done`` for that turn",
    )

    @pytest.mark.parametrize("claim", RETIRED)
    def test_no_source_asserts_the_retired_formulation(self, claim):
        assert _offenders(claim) == []

    #: The SIX sites, named individually rather than swept.
    #:
    #: A whole-file `"evidence" in source` check is not a pin here: `client.py`
    #: and `confirm.py` both use that word many times for unrelated reasons, so
    #: two of the six would have passed without being touched. Each docstring is
    #: addressed by the symbol that owns it.
    ANCHOR_DOCS = (
        ("client module", lambda: __import__(
            "agentwire.voice_layer.client", fromlist=["x"]).__doc__),
        ("server module", lambda: server.__doc__),
        ("transcript module", lambda: __import__(
            "agentwire.voice_layer.transcript", fromlist=["x"]).__doc__),
        ("server.BuddyBridge.anchor", lambda: server.BuddyBridge.anchor.__doc__),
        ("confirm.ConfirmSpine.announce", lambda: confirm.ConfirmSpine.announce.__doc__),
        ("confirm.Proposal", lambda: confirm.Proposal.__doc__),
    )

    @pytest.mark.parametrize("name,getter", ANCHOR_DOCS, ids=[n for n, _ in ANCHOR_DOCS])
    def test_the_anchor_docstring_names_the_mechanism_it_is_driven_from(self, name, getter):
        """`onSpoken`, not "fallback".

        The first version of this asserted the word "fallback" — and
        `client.py`'s docstring already contained it, for an unrelated reason
        (the REFUSAL fallback), so that one param was green before the fix and
        counted as coverage anyway. `onSpoken` is the thing that actually
        distinguishes the corrected claim from the retired one: it is what
        `/anchor` is POSTed from, and it fires for both `how` values. Verified
        absent from all six docstrings before the fix.
        """
        doc = _flat(getter() or "")
        assert "onSpoken" in doc, name


class TestWaitIsNotUnconditionalOnEitherSide:
    """#987 added `("cant","wait")`, so the absolute is false — and the first
    round of pins forbade it on the PAGE only, leaving the identical claim in
    `confirm.py` one file away."""

    def test_no_source_says_wait_denies_unconditionally(self):
        assert _offenders("wait`` denies unconditionally") == []
        assert _offenders("`wait` denies unconditionally") == []


class TestNothingCallsTheSurfaceReadOnly:
    """The write path has shipped. `buddy_cli` and `INDEX.md` were fixed in
    round one; `voice_layer/__init__.py` was not."""

    #: The exact retired strings, per surface. NOT a blanket ban on the words
    #: "read-only tool surface": the page uses that phrase correctly in the past
    #: tense ("Until it landed, … WAS a theoretical statement about a read-only
    #: tool surface"), and a pin that forbids the accurate history along with
    #: the stale claim gets deleted by the next person rather than obeyed.
    RETIRED_BY_FILE = (
        ("agentwire/voice_layer/__init__.py", "read-only fleet-awareness tool surface"),
        ("agentwire/buddy_cli.py", "This slice is READ-ONLY"),
        ("agentwire/buddy_cli.py", "Show the read-only tool surface"),
        ("docs/wiki/INDEX.md", "read-only fleet awareness"),
    )

    @pytest.mark.parametrize(
        "relative,claim", RETIRED_BY_FILE, ids=[f"{f}:{c[:24]}" for f, c in RETIRED_BY_FILE]
    )
    def test_the_retired_read_only_claim_is_gone(self, relative, claim):
        root = Path(__file__).resolve().parents[2]
        text = _flat((root / relative).read_text(encoding="utf-8"))
        assert _every_occurrence_is_a_correction(text, claim), relative

    def test_the_package_docstring_names_the_gated_write(self):
        import agentwire.voice_layer as package

        doc = _flat(package.__doc__ or "")
        assert "write_tools" in doc
        assert "confirm" in doc


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
        # Context-aware like its siblings: a page that RETIRES this sentence has
        # to be able to quote it. The plain `not in` version forbade the
        # correction along with the defect.
        assert _every_occurrence_is_a_correction(page, "`wait` denies unconditionally")

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

    def test_the_shipped_browser_page_has_both_legs(self):
        """Derived from the BROWSER page, not the wiki.

        Renamed: this used to be `test_the_page_matches_the_shipped_gate` with a
        local named `page`, which is also the wiki fixture's name one class up —
        so it read as a doc-agreement pin and was counted as one. It is not: it
        asserts `canInterrupt`'s two legs exist in the page source the browser
        actually gets, and it would pass with no wiki at all.
        """
        from agentwire.voice_layer import client

        browser_page = client.page("buddy", "token")
        assert "!announcer.anchorPending()" in browser_page
        assert "!confirmGate.outstanding()" in browser_page


class TestTheOpenResidualsAreNamed:
    """A wiki that describes what we meant is how the next contributor argues
    from a mechanism that does not exist.

    #989, #990 and #992 were removed from this list when they were closed —
    leaving them would have been the same defect with its polarity flipped, and
    for one round it WAS: a paragraph 240 lines above the section that closes
    #992 still called it open and still recommended the two mitigations that
    section rejects. `TestTheResidualTableDoesNotAdvertiseClosedHoles` is the
    pin for that direction, and it reads prose, not only table rows.

    #995/#996/#997 left the same way in #1007, so #1009 is what remains.
    """

    @pytest.mark.parametrize("issue", [1009])
    def test_residual_is_recorded(self, page, issue):
        assert f"#{issue}" in page

    def test_the_ones_that_shipped_a_fix_are_not_in_this_list(self):
        """The two lists are complements, asserted rather than assumed —
        an issue in both is a page contradicting itself."""
        closed = set(TestTheResidualTableDoesNotAdvertiseClosedHoles.CLOSED)
        assert "#1009" not in closed

    def test_the_page_does_not_call_onNotSpoken_a_positive_report_only(self, page):
        """The wiki carried the same sentence ``client.py``'s handler did —
        "positive evidence ... reached only from `speechSynthesis`'s own
        `onerror`" — and #996 made the watchdog a second caller whose evidence
        is an INFERENCE, not a report. Left alone it contradicted this page's
        own #996 bullet twenty-five lines above it, in the same section."""
        assert "reached only from `speechSynthesis`'s own `onerror`" not in page
        assert "two callers carrying different kinds of evidence" in page
        assert "a guess, made in one direction on purpose" in page

    @pytest.mark.parametrize("issue", [995, 996, 997])
    def test_a_closed_one_is_not_still_listed_as_a_hole(self, page, issue):
        """The other direction, and it is not symmetric with the assertion
        above: a residual listed after it is closed sends the next contributor
        to build around a hole that is not there, and this table is the one
        place they would look. These three were closed together; the row is
        gone and the closure is recorded instead."""
        table = page.split("| # | Where | The hole |", 1)[1].split("**Closed", 1)[0]
        # Keyed on the ROW, not on the mention. #1009's row describes the debt
        # #997's fix left behind and so names it — a bare substring check read
        # that as "#997 is still listed", which is the pin failing for a reason
        # that has nothing to do with what it guards.
        assert f"| #{issue} |" not in table
        assert f"#{issue}" in page


class TestTheDeliverySeamNoLongerClaimsItNeverInterrupts:
    """True of the seam, false of the layer, once #962/#967 landed."""

    def test_the_page_records_the_clock(self, page):
        assert "polls the spool every 5s" in page

    def test_the_module_docstring_agrees(self):
        from agentwire.voice_layer import delivery

        doc = _flat(delivery.__doc__ or "")
        assert "stopped being true of the layer" in doc
