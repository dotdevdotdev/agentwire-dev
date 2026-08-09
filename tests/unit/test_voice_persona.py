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
"""

from agentwire.voice_layer import instructions


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


class TestSpeakability:
    def test_the_persona_is_speech_not_markup(self):
        """It is read to a realtime voice model: no markdown bullets, no
        backticks, no identifiers to spell."""
        persona = instructions.PERSONA
        body = persona.replace("<persona>", "").replace("</persona>", "")
        assert "`" not in body
        assert "- " not in body  # no bullet lists in a spoken prompt
        assert "response.create" not in body
