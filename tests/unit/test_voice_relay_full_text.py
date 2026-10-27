"""The FULL relayed utterance reaches the recipient (#1015).

The measured failure: every relay in the first live voice session arrived cut
off mid-sentence (``"Treat it as a running list for anyt…"``) and the receiving
session acted on the fragment. The body's caps are not raisable — they are the
measured #689 boundary — so the fix is a second channel: the whole request on
disk, a pointer to it on the delivered line, and the inline text demoted to a
preview.

What each test here is worth is stated as the failure it would let through, not
as "the pointer is present": a pointer that names a file that was never written,
or that a budget squeeze dropped, or that a spine-level path never actually
freezes into the argv, all leave the owner exactly where #1015 found them —
with a session acting on half a sentence.
"""


from agentwire.voice_layer import confirm, relay

#: Long enough that every slot clips: the shape the live session produced.
LONG_INSTRUCTION = (
    "Treat it as a running list for anything I say about voice mode. "
    "Collect the feedback as it comes, keep each item in my own words, and "
    "do not start acting on any of it until I have finished the whole pass "
    "and told you explicitly to begin — some of these will contradict each "
    "other and I want to reconcile them myself before anything ships."
)
LONG_UTTERANCE = (
    "okay so treat it as a running list for anything I say about voice mode "
    "and just keep collecting until I tell you to start on it"
)


def relay_of(body: str) -> str:
    """The path the body's ``full:`` slot points at, or ``""``."""
    for part in body.split(confirm.SEP):
        if part.startswith(confirm.POINTER_LABEL):
            return part[len(confirm.POINTER_LABEL):]
    return ""


class TestTheFullTextSurvives:
    def test_the_relay_file_holds_the_whole_instruction_and_utterance(self, tmp_path):
        """The property the issue is about. If this fails, the text the body
        clipped exists nowhere and the recipient cannot recover it."""
        path = relay.relay_path("a1b2c3")
        written = relay.write_relay(
            path,
            proposal_id="a1b2c3",
            session="orchestrator",
            sender="buddy",
            instruction=LONG_INSTRUCTION,
            request_utterance=LONG_UTTERANCE,
        )
        assert written == str(path)
        text = path.read_text(encoding="utf-8")
        assert LONG_INSTRUCTION in text
        assert LONG_UTTERANCE in text
        assert "a1b2c3" in text


class TestThePointerRidesExactlyWhenItIsNeeded:
    def test_a_clipped_instruction_gets_a_pointer(self):
        body = confirm.render_body(
            LONG_INSTRUCTION, "", "a1b2c3", full_path="/tmp/relays/a1b2c3.md"
        )
        assert relay_of(body) == "/tmp/relays/a1b2c3.md"
