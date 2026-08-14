"""The persona is legible from the repo, and its properties are pinned (#967).

Spoken-surface strings are the category that never gets tests naturally — a
wrong sentence and a right sentence are structurally identical at review time
(the voice-layer doc's maintenance note). The persona is the largest spoken
surface of all, so its load-bearing properties are asserted rather than left
as prose someone can soften in passing:

- the peer stance names the failure mode it designs against (the deferential
  narrator), not just the virtue it wants;
- the humour rule is a countable BUDGET a model can follow, not an adjective;
- opinion and fact are distinguished by ownership, composing with the #956
  epistemic boundary instead of contradicting it;
- insistence is described as the second attempt, and the interrupt decision is
  explicitly located in code — prompt compliance is not a gate;
- the support-layer boundary survives verbatim in spirit: never writes code,
  never owns a worktree, never creates a session.

#980 adds the other half of that job: a sentence in this prompt is a guarantee
the buddy speaks and acts on, so where the prose states something broader than
the code enforces, the prose is wrong. Three of those are pinned below AGAINST
THE CODE rather than against their own wording — the volunteering set (which
the client drives, not the model), the limits sentence (which must derive from
what is wired, not assert a negative the next wiring falsifies), and the reply
nudge (which ``render_body`` drops whole whenever the body runs long).
"""

import inspect

from agentwire.voice_layer import confirm, instructions, surface, write_tools


def source() -> str:
    """The module's own text, comments included.

    The comments above PERSONA and VOICE_MODE describe what the prompt
    strings say. They are prose about prose, and they drift the same way the
    prompt does — twice already: one described the re-raise sentence it had
    just been rewritten past, the other claimed a phrasing the shipped
    sentence did not have. Nothing reads a comment at runtime, so pinning
    them needs the source.
    """
    return inspect.getsource(instructions)


def flowed_comments() -> str:
    """:func:`source` with the ``#:`` markers and line wrapping flowed away,
    so an assertion about what a comment SAYS does not also pin where the
    72-column wrap happens to fall."""
    return " ".join(source().replace("#:", " ").split())


def full() -> str:
    return instructions.build_instructions()


class TestThePersonaIsFirstClass:
    def test_it_is_a_named_section_between_base_and_voice_mode(self):
        text = full()
        assert "<persona>" in text and "</persona>" in text
        # Order: identity first, persona second, channel mechanics last.
        assert text.index("<persona>") < text.index("<voice_mode>")
        assert "voice buddy for agentwire" in text.split("<persona>")[0]

    def test_extra_still_appends_after_everything(self):
        text = instructions.build_instructions(extra="EXTRA-MARKER")
        assert text.rstrip().endswith("EXTRA-MARKER")


class TestThePeerStance:
    def test_it_names_the_register_to_avoid_not_only_the_one_to_want(self):
        """"Be a peer" alone is an adjective; the failure mode is the
        deferential narrator, and the text has to name it so a future edit
        that reintroduces it is visibly wrong."""
        persona = instructions.PERSONA
        assert "peer, not an assistant" in persona
        assert "deferential" in persona
        assert "status report" in persona

    def test_opinions_are_allowed_to_be_wrong_out_loud(self):
        assert "wrong out loud" in instructions.PERSONA


class TestOpinionVsFact:
    """The composition requirement: a peer with opinions must still never
    invent facts. Distinguished by OWNERSHIP, which is checkable."""

    def test_the_distinction_is_stated_with_a_worked_pair(self):
        persona = instructions.PERSONA
        assert "OPINIONS ARE NOT FACTS" in persona
        # A worked example of each side, so the rule is followable rather
        # than aspirational.
        assert "is an opinion" in persona
        assert "is a fact" in persona

    def test_facts_are_looked_up_never_remembered(self):
        assert "looked up, never remembered" in instructions.PERSONA

    def test_it_does_not_contradict_the_epistemic_boundary(self):
        """#956's BOUNDARY section survives untouched in voice_mode; the
        persona must not grant what it forbids."""
        text = full()
        assert "Never invent a mechanism" in text
        assert "never dress up a guess" in text


class TestTheHumourBudget:
    def test_it_is_countable_not_an_adjective(self):
        """"Be witty" is not implementable. "At most one dry aside, riding on
        a sentence that had to be said anyway" is."""
        persona = instructions.PERSONA
        assert "HUMOUR IS A BUDGET" in persona
        assert "at most one" in persona
        assert "had to be said" in persona

    def test_it_prices_the_voice_channel_specifically(self):
        """The reason the budget is small: a spoken joke cannot be skimmed."""
        persona = instructions.PERSONA
        assert "cannot be skimmed" in persona
        assert "wait it out" in persona

    def test_it_names_when_to_spend_nothing(self):
        assert "spend nothing" in instructions.PERSONA


class TestInsistenceAndTheBoundary:
    def test_insistence_is_the_second_attempt_not_volume(self):
        persona = instructions.PERSONA
        assert "second mention" in persona
        assert "second attempt" in persona
        # Bounded: twice, then leave it.
        assert "Twice is a peer" in persona

    def test_the_interrupt_decision_is_located_in_code_not_in_the_prompt(self):
        """The same reason the confirm judgment lives below the model: prompt
        compliance is not a mechanism. The prose says the timing is decided in
        code, so a reader cannot conclude the model self-grants urgency."""
        text = full()
        assert "decided in code" in text

    def test_never_over_the_owner_survives_as_the_unconditional_leg(self):
        """#962 reconciliation, prompt side: the sentence narrowed from a bare
        "never interrupt" to "never interrupt THE OWNER" — the leg that stays
        unconditional for every tier, while the code-side tier may pre-empt
        the buddy's own speech."""
        text = full()
        assert "never interrupt the owner" in text

    def test_the_support_layer_boundary_is_stated_in_full(self):
        persona = instructions.PERSONA
        assert "never write code" in persona
        assert "never own a worktree" in persona
        assert "never create a session" in persona


class TestVolunteeringMatchesTheCodeDrivenSet:
    """#980 defect 1. VOICE_MODE said "exactly one thing is worth raising
    unprompted … beyond that one case, do not volunteer" while the client
    ships three unprompted paths and PERSONA instructs a second mention. Each
    text was correct when written; the composition was not."""

    def test_it_no_longer_claims_a_single_sanctioned_case(self):
        text = full()
        assert "Exactly one thing is worth raising unprompted" not in text
        assert "Beyond that one case" not in text

    def test_all_three_code_driven_paths_are_named(self):
        """The client's unprompted speech: an inbox notice on a quiet
        full-gate tick, a re-raise reminder on a quiet full-gate tick, and an
        escalation through the relaxed interrupt gate."""
        text = full()
        # 1. mail arriving — and NOT narrowed to replies to what you sent:
        #    buddy_inbox is "reports and requests other sessions have sent
        #    YOU", whoever started the exchange.
        assert "mail arriving" in text
        # 2. the code-scheduled second mention.
        assert "second mention" in instructions.VOICE_MODE
        # 3. the one kind that may pre-empt the buddy's own speech.
        assert "escalation" in instructions.VOICE_MODE

    def test_the_set_is_closed_and_owned_by_code(self):
        """The model must not read the list as examples it may extend."""
        text = instructions.VOICE_MODE
        assert "the list is closed" in text
        assert "You do not add to that list" in text

    def test_the_re_raise_leg_is_the_one_the_ledger_actually_schedules(self):
        """The ledger registers ONLY request/escalation kinds (a done or a
        note is news, and re-raising news is chatter), and it fires after the
        item has stayed open — not on the model noticing."""
        text = instructions.VOICE_MODE
        assert "asked for action" in text
        assert "still open" in text

    def test_the_persona_does_not_put_the_second_mention_in_the_models_hands(self):
        """PERSONA and VOICE_MODE must agree on WHO decides the re-raise; the
        ledger does, the model only speaks it."""
        persona = instructions.PERSONA
        assert "you can see it is still true" not in persona
        assert "handed to you" in persona
        assert "scheduled for you rather than left to" in persona

    def test_the_comment_above_persona_agrees_with_the_paragraph(self):
        """#991 review. The comment still described the deleted sentence —
        "say it once more at a gap" if "it is visibly still true later", which
        is the model judgment the ledger took over. A comment that documents
        the previous draft is how the next reader restores it."""
        src = source()
        assert "visibly still true later" not in src
        assert "The ledger decides when" in flowed_comments()


class TestLimitsDeriveFromWhatIsWired:
    """#980 defect 2: "you cannot … stop one" is true of today's wiring and
    false of the tier ruling, and nothing re-labels prose when a spec ships."""

    def test_kill_is_wireable_so_the_prose_must_not_deny_it_categorically(self):
        """The premise, asserted rather than assumed: session_kill is a graded
        gated write, not an exclusion, so a sentence saying the buddy cannot
        stop a session is a claim the next spec silently falsifies."""
        assert "session_kill" in surface.TIER_WRITE_GATED
        assert "session_kill" not in surface.TIER_EXCLUDED
        assert "session_kill" not in {spec.name for spec in write_tools.WRITE_SPECS}
        assert "stop one" not in instructions.VOICE_MODE

    def test_capability_is_stated_positively_against_the_tool_list(self):
        text = instructions.VOICE_MODE
        assert "Your tools are the whole of what you can do" in text
        assert "if none does, you cannot" in text

    def test_the_permanent_exclusions_it_still_states_really_are_excluded(self):
        """What survives as an absolute must be absolute IN THE TIER TABLE —
        a design decision, not a wiring accident."""
        for name in ("session_create", "worktree_create", "say", "email_send",
                     "desktop_write_artifact"):
            assert name in surface.TIER_EXCLUDED
        text = instructions.VOICE_MODE
        assert "never start" in text
        assert "never write code" in text
        assert "another channel" in text
        assert "outside the fleet" in text


class TestTheConfirmSentenceDoesNotQuantifyOverEveryWrite:
    """#991 review. The repair for defect 2 shipped its own copy of the
    defect: "A tool that changes something tells you so by handing back a
    confirm phrase" quantifies over ALL mutating tools, and the light grade is
    confirm-free BY DESIGN. True only while no light write is wired — which is
    a wiring accident, exactly the thing this paragraph stopped relying on."""

    def test_the_light_grade_is_confirm_free_and_unwired_by_accident(self):
        """The premise. Light writes are graded, disjoint from gated, and
        absent from WRITE_SPECS only because none has a CLI verb yet."""
        assert surface.TIER_WRITE_LIGHT
        assert not (surface.TIER_WRITE_LIGHT & surface.TIER_WRITE_GATED)
        wired = {spec.name for spec in write_tools.WRITE_SPECS}
        assert not (surface.TIER_WRITE_LIGHT & wired)

    def test_the_prose_says_some_not_all(self):
        text = instructions.VOICE_MODE
        assert "A tool that changes something tells you so" not in text
        assert "Some of them ask first" in text

    def test_the_comment_describes_the_sentence_that_shipped(self):
        """The second half of the same finding: the comment claimed a
        phrasing ("what a gated tool DOES") the string did not have, and
        "gated" is a word the prompt never defines to the model."""
        src = source()
        assert "deliberately phrased as what a gated tool DOES" not in src
        comments = flowed_comments()
        assert "the quantifier is the point" in comments
        assert "confirm-free BY DESIGN" in comments
        # And the reason naming the grade would not have rescued it.
        assert "a word this prompt never defines to the model" in comments


class TestTheReplyPathIsConditional:
    """#980 defect 3: the nudge is droppable, the sentence was not."""

    def test_the_code_really_does_drop_the_nudge_on_a_long_body(self):
        """The premise, demonstrated rather than cited — if render_body ever
        makes the nudge unconditional, this test says so and the prose below
        becomes the thing that needs changing."""
        short = confirm.render_body("ship it", "", "abc123", reply_to="worker")
        assert "reply: agentwire msg send" in short
        long = confirm.render_body(
            "x" * confirm.MAX_RENDERED_INSTRUCTION_CHARS,
            "y" * confirm.MAX_UTTERANCE_CHARS,
            "abc123",
            reply_to="a-rather-long-worktree-session-name-here",
        )
        assert len(long) <= confirm.MAX_BODY_CHARS
        assert "reply: agentwire msg send" not in long

    def test_the_sentence_is_narrowed_not_qualified(self):
        text = instructions.VOICE_MODE
        assert "Your messages also carry the reply path" not in text
        assert "When there is room for it" in text
        assert "not a guarantee" in text

    def test_it_points_at_the_record_that_can_settle_it(self):
        """buddy_sent records the exact body that went out, so "was the
        recipient told how to answer" is a lookup, not a belief."""
        text = instructions.VOICE_MODE
        nudge = text.split("When there is room for it")[1].split("\n\n")[0]
        assert "buddy_sent" in nudge


class TestTheWaitInstructionKeysOnTheFlagNotTheOutcomeNames:
    """W1 added a THIRD wait outcome (in_flight). This prompt survived that
    change unedited because it consumes the FLAG; the test pins the reason so
    a future edit cannot quietly reintroduce an enumeration that goes stale."""

    def test_the_flag_is_what_the_prose_names(self):
        assert "owner_should_wait" in instructions.VOICE_MODE

    def test_no_wait_outcome_is_named_in_the_prose(self):
        for outcome in confirm.WAIT_OUTCOMES:
            assert outcome not in instructions.VOICE_MODE
        # A COUNTER, not a cap: the assertion above is the guarantee, and this
        # line only forces a reader to re-check it whenever the set grows.
        # in_flight arrived in W1; cancel_in_flight with #990.
        assert len(confirm.WAIT_OUTCOMES) == 4


class TestSpeakability:
    def test_the_persona_is_speech_not_markup(self):
        """It is read to a realtime voice model: no markdown bullets, no
        backticks, no identifiers to spell."""
        persona = instructions.PERSONA
        body = persona.replace("<persona>", "").replace("</persona>", "")
        assert "`" not in body
        assert "- " not in body  # no bullet lists in a spoken prompt
        assert "response.create" not in body


class TestGateFirstThenSpeak:
    """#1039 part 1. Observed live: the buddy said "ok great, I'll send it
    out" BEFORE the matcher ruled, then contradicted itself one line later.
    The prompt must forbid pre-acknowledgment, and the outcome line must be
    the tool result's own "say" — the spine's spoken template — never the
    model's optimistic guess. Prose is not the mechanism (the gate is), but a
    prompt that INVITES the contradiction is a defect of its own."""

    def test_the_rule_is_present_and_orders_gate_before_speech(self):
        text = instructions.VOICE_MODE
        assert "GATE FIRST, THEN SPEAK" in text
        assert "until send_session_message has returned" in text

    def test_the_outcome_line_is_the_tool_results_say(self):
        assert 'speak the "say" line the result hands back' in (
            instructions.VOICE_MODE
        )

    def test_it_forbids_the_observed_pre_acknowledgment_shape(self):
        text = instructions.VOICE_MODE
        assert "do not acknowledge it" in text
        # The observed contradiction, named so the rule stays anchored to it.
        assert "I'll send it out" in text

    def test_the_near_miss_relay_is_named(self):
        """The heard-word diagnosis only helps if the model passes it on
        verbatim rather than paraphrasing it away."""
        assert "names what was heard" in instructions.VOICE_MODE
