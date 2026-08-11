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

import os
import time
from pathlib import Path

import pytest

from agentwire import core, inbox
from agentwire.voice_layer import confirm, relay, transcript, write_tools

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
LONG_SENDER = "agentwire-dev-voice-confirm-spine"


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

    def test_the_body_points_at_a_file_that_exists_and_holds_the_rest(self):
        """The two halves agree. A pointer computed one way and a file written
        another is the same defect wearing a fix."""
        proposal_id = "a1b2c3"
        path = relay.relay_path(proposal_id)
        written = relay.write_relay(
            path,
            proposal_id=proposal_id,
            session="orchestrator",
            sender="buddy",
            instruction=LONG_INSTRUCTION,
            request_utterance=LONG_UTTERANCE,
        )
        body = confirm.render_body(
            LONG_INSTRUCTION, LONG_UTTERANCE, proposal_id, full_path=written
        )
        pointed = relay_of(body)
        assert pointed == str(path)
        assert Path(pointed).exists()
        # The excerpt really is an excerpt — otherwise this test would pass on
        # a body that never needed the pointer.
        assert body.split(confirm.SEP)[0].rstrip("…") != LONG_INSTRUCTION
        assert LONG_INSTRUCTION in Path(pointed).read_text(encoding="utf-8")

    def test_the_relay_is_written_whole_or_not_at_all(self, monkeypatch):
        """A partial relay is a silently partial instruction — the defect, not
        a degraded version of the fix."""
        import agentwire.voice_layer.relay as relay_module

        seen = {}
        real_replace = Path.replace

        def spy(self, target):
            seen["temp"] = self.read_text(encoding="utf-8")
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", spy)
        path = relay.relay_path("a1b2c3")
        relay.write_relay(
            path,
            proposal_id="a1b2c3",
            session="s",
            sender="buddy",
            instruction=LONG_INSTRUCTION,
            request_utterance="",
        )
        assert LONG_INSTRUCTION in seen["temp"]
        assert relay_module.RETENTION_DAYS > 0


class TestThePointerRidesExactlyWhenItIsNeeded:
    def test_a_clipped_instruction_gets_a_pointer(self):
        body = confirm.render_body(
            LONG_INSTRUCTION, "", "a1b2c3", full_path="/tmp/relays/a1b2c3.md"
        )
        assert relay_of(body) == "/tmp/relays/a1b2c3.md"

    def test_a_clipped_utterance_gets_one_too(self):
        """The ``said:`` slot clips at 90 chars. That clip is a paraphrase-check
        failure, not a cosmetic one — the recipient checks the buddy's wording
        against the owner's, and a truncated quote cannot be checked."""
        body = confirm.render_body(
            "restart the portal", LONG_UTTERANCE, "a1b2c3",
            full_path="/tmp/relays/a1b2c3.md",
        )
        assert relay_of(body) == "/tmp/relays/a1b2c3.md"

    def test_a_body_that_lost_nothing_carries_no_pointer(self):
        """Both halves priced. ~50 characters of a 300-character line spent on
        a pointer to text already on screen is the excerpt and the reply nudge
        paid for nothing."""
        body = confirm.render_body(
            "restart the portal", "confirm tango", "a1b2c3",
            full_path="/tmp/relays/a1b2c3.md",
        )
        assert relay_of(body) == ""
        assert body == confirm.render_body(
            "restart the portal", "confirm tango", "a1b2c3"
        )

    def test_the_short_body_is_unchanged_from_before_the_fix(self):
        """The common case is byte-identical, so #1015 cannot have quietly
        re-shaped every message on the way to fixing the long ones."""
        body = confirm.render_body(
            "restart the portal", "confirm tango", "a1b2c3", reply_to="buddy"
        )
        assert body == (
            'restart the portal ┃ said: "confirm tango" ┃ '
            'reply: agentwire msg send --to buddy --kind done "<answer>" ┃ #a1b2c3'
        )


class TestThePointerIsNotDroppable:
    """The reply nudge is droppable; this is not. Dropping the pointer under
    budget pressure drops the fix — silently, on exactly the longest messages,
    which are the ones that needed it."""

    @pytest.mark.parametrize("instruction_len", [161, 300, 600, 5000])
    @pytest.mark.parametrize("utterance_len", [0, 91, 5000])
    def test_the_pointer_and_the_id_both_survive_every_length(
        self, instruction_len, utterance_len
    ):
        path = "/Users/dotdev/.agentwire/voice/relays/a1b2c3.md"
        body = confirm.render_body(
            "x" * instruction_len,
            "y" * utterance_len,
            "a1b2c3",
            reply_to=LONG_SENDER,
            full_path=path,
        )
        assert relay_of(body) == path, body
        assert body.endswith("#a1b2c3"), body
        assert len(body) <= confirm.MAX_BODY_CHARS, len(body)

    def test_the_delivered_line_still_clears_the_measured_paste_boundary(self):
        """The pointer must not spend the margin the pane measurement bought:
        past ``MEASURED_STUCK_LIMIT_CHARS`` the #689 heal stops firing and a
        swallowed Enter wedges the message permanently."""
        body = confirm.render_body(
            "x" * 5000, "y" * 5000, "a1b2c3",
            reply_to=LONG_SENDER,
            full_path="/Users/dotdev/.agentwire/voice/relays/a1b2c3.md",
        )
        rendered = inbox.Message(
            id="1700000000000000000-abc123",
            sender=LONG_SENDER,
            to="orchestrator",
            kind=write_tools.WRITE_KIND,
            text=body,
            ts=1700000000000,
        ).render()
        assert len(rendered) <= confirm.MEASURED_STUCK_LIMIT_CHARS
        assert len(rendered) <= confirm.MEASURED_STUCK_LIMIT_CHARS * 0.8
        assert "\n" not in rendered and "\r" not in rendered

    def test_the_verbatim_quote_yields_to_the_pointer_not_the_other_way(self):
        """The ordering ruling, at the length where it bites (a deep ``$HOME``
        — which is also what pytest's tmp home produces, so this branch is not
        hypothetical). The quote is reproduced in the file the pointer names;
        the pointer is the only copy of everything else. Recoverable yields."""
        deep = (
            "/private/var/folders/1d/g63f4vld5x79q6m_swhxjpb00000gn/T/"
            "pytest-of-dotdev/pytest-3119/home1"
        )
        path = f"{deep}/.agentwire/voice/relays/a1b2c3.md"
        assert (
            confirm.MAX_BODY_CHARS
            - len(f"{confirm.POINTER_LABEL}{path}")
            - len('said: ""') - confirm.MAX_UTTERANCE_CHARS
            - len("#a1b2c3")
            - 3 * len(confirm.SEP)
        ) < confirm.MIN_EXCERPT_CHARS, "fixture must actually squeeze the budget"

        body = confirm.render_body(
            LONG_INSTRUCTION, LONG_UTTERANCE, "a1b2c3", full_path=path
        )
        assert relay_of(body) == path
        assert "said:" not in body
        assert len(body.split(confirm.SEP)[0]) >= confirm.MIN_EXCERPT_CHARS
        # And the dropped quote is not lost — it is in the file.
        written = relay.write_relay(
            relay.relay_path("a1b2c3"),
            proposal_id="a1b2c3",
            session="s",
            sender="buddy",
            instruction=LONG_INSTRUCTION,
            request_utterance=LONG_UTTERANCE,
        )
        assert LONG_UTTERANCE in Path(written).read_text(encoding="utf-8")

    def test_an_unusably_long_path_yields_to_the_excerpt(self):
        """The other direction of the same guard. A pointer that leaves no
        scannable preview buys recoverability nobody goes looking for."""
        body = confirm.render_body(
            LONG_INSTRUCTION, LONG_UTTERANCE, "a1b2c3", full_path="/" + "d" * 400
        )
        assert relay_of(body) == ""
        assert len(body) <= confirm.MAX_BODY_CHARS
        assert body.endswith("#a1b2c3")
        assert len(body.split(confirm.SEP)[0]) >= confirm.MIN_EXCERPT_CHARS


class TestFailureDegradesToTodaysBehaviour:
    def test_an_unwritable_store_never_raises(self, monkeypatch, tmp_path):
        """``write_relay`` is called after ``_proposals.pop()`` and outside the
        runner's ``try``: a raise there eats an approved message with no screen
        to report it on (the ``_lead_safe`` lesson, same position)."""
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        monkeypatch.setattr(relay, "relay_dir", lambda: blocked / "relays")
        written = relay.write_relay(
            blocked / "relays" / "a1b2c3.md",
            proposal_id="a1b2c3",
            session="s",
            sender="buddy",
            instruction=LONG_INSTRUCTION,
            request_utterance="",
        )
        assert written == ""

    def test_a_failed_write_drops_the_flag_as_well_as_the_slot(self, monkeypatch):
        """A pointer to a file that is not there is worse than no pointer: the
        recipient reads a missing path as "the real instruction is elsewhere"
        and stops."""
        monkeypatch.setattr(relay, "write_relay", lambda *a, **k: "")
        ref = str(relay.relay_path("a1b2c3"))
        proposal = confirm.Proposal(
            id="a1b2c3",
            token="t",
            nonce="tango",
            tool="send_session_message",
            session="orchestrator",
            instruction=LONG_INSTRUCTION,
            argv_prefix=(
                "msg", "send", "--to", "orchestrator", "--from", "buddy",
                "--kind", "voice", "--ref", ref,
            ),
            created_at=0.0,
        )
        argv = proposal.build_argv()
        assert "--ref" not in argv
        assert ref not in argv
        assert relay_of(argv[-1]) == ""
        assert argv[-1].endswith("#a1b2c3")

    def test_a_foreign_ref_never_steers_the_write(self, tmp_path):
        """The pointer is matched against the path the id DERIVES, not merely
        read out of the argv — so a future spec freezing a ``--ref`` of its own
        cannot turn ``build_argv`` into "open the path in the argv"."""
        foreign = tmp_path / "attacker.md"
        proposal = confirm.Proposal(
            id="a1b2c3",
            token="t",
            nonce="tango",
            tool="send_session_message",
            session="orchestrator",
            instruction=LONG_INSTRUCTION,
            argv_prefix=(
                "msg", "send", "--to", "orchestrator", "--from", "buddy",
                "--kind", "voice", "--ref", str(foreign),
            ),
            created_at=0.0,
        )
        argv = proposal.build_argv()
        assert not foreign.exists()
        assert relay_of(argv[-1]) == ""

    def test_relay_path_refuses_anything_that_is_not_a_proposal_id(self):
        """The path is fixed-length and traversal-free by CONSTRUCTION (the id
        is ``token_hex(3)``). This asserts the construction, so a future caller
        that hands it a name cannot quietly turn it into a path."""
        for bad in ("", "../../etc/passwd", "a1b2c3/../x", "Z" * 6, "a1b2c3.md"):
            with pytest.raises(ValueError):
                relay.relay_path(bad)


class TestTheStoreIsBounded:
    def test_old_relays_are_pruned_on_write(self):
        stale = relay.relay_path("dead01")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("old")
        old = time.time() - (relay.RETENTION_DAYS + 1) * 86400
        os.utime(stale, (old, old))

        fresh = relay.relay_path("a1b2c3")
        relay.write_relay(
            fresh,
            proposal_id="a1b2c3",
            session="s",
            sender="buddy",
            instruction="hi",
            request_utterance="",
        )
        assert not stale.exists()
        assert fresh.exists()

    def test_the_store_lives_under_the_config_dir_at_call_time(self, monkeypatch):
        """A FUNCTION, not an import-time constant: a constant ignores a
        patched CONFIG_DIR, which is how a test seam becomes a write against
        the owner's real store (#871's ``role_prompts_dir``)."""
        monkeypatch.setattr(core, "CONFIG_DIR", Path("/somewhere/else"))
        assert relay.relay_dir() == Path("/somewhere/else/voice/relays")


class TestTheWikiStatesWhatShipped:
    """The page is what a future session reads before touching the caps. A
    sentence there that outruns the code gets rounded back up, so the two
    load-bearing claims are pinned against the code, not just written down."""

    @pytest.fixture
    def page(self):
        return " ".join(
            (
                Path(__file__).resolve().parents[2]
                / "docs" / "wiki" / "voice-layer.md"
            ).read_text(encoding="utf-8").split()
        )

    def test_the_priority_ruling_is_recorded(self, page):
        assert "Recoverable yields to unrecoverable." in page
        assert "The pointer is not droppable" in page

    def test_the_residual_is_stated_rather_than_implied(self, page):
        """``MAX_INSTRUCTION_CHARS`` still refuses a long proposal outright.
        The page must not let "the full utterance reaches the session" imply
        that limit went away."""
        assert "MAX_INSTRUCTION_CHARS = 600" in page
        assert write_tools.MAX_INSTRUCTION_CHARS == 600


class TestEndToEndThroughTheSpine:
    """The layers agree. Each half can be right while the pair is broken: a
    renderer that takes a path nobody passes, or an argv whose ``--ref`` names
    a file nothing writes."""

    @pytest.fixture
    def spine(self):
        return confirm.ConfirmSpine(transcript.TranscriptRing(), wait_s=0.0)

    def test_the_argv_carries_the_ref_and_the_file_holds_the_full_text(self, spine):
        proposal = spine.propose(
            tool="send_session_message",
            session="orchestrator",
            instruction=LONG_INSTRUCTION,
            argv_prefix=[
                "msg", "send", "--to", "orchestrator", "--from", "buddy",
                "--kind", write_tools.WRITE_KIND,
            ],
        )
        assert "--ref" in proposal.argv_prefix, (
            "the pointer is frozen at PROPOSE time — freezing it later would "
            "reopen 'the whole argv is frozen at propose' (#966)"
        )
        argv = proposal.build_argv()
        ref = argv[argv.index("--ref") + 1]
        assert ref == str(relay.relay_path(proposal.id))
        assert Path(ref).read_text(encoding="utf-8").count(LONG_INSTRUCTION) == 1
        assert relay_of(argv[-1]) == ref

    def test_nothing_is_written_for_a_proposal_that_never_executes(self, spine):
        """The file is written at execution, not at propose: a cancelled or
        expired proposal must leave nothing on disk."""
        proposal = spine.propose(
            tool="send_session_message",
            session="orchestrator",
            instruction=LONG_INSTRUCTION,
            argv_prefix=[
                "msg", "send", "--to", "orchestrator", "--from", "buddy",
                "--kind", write_tools.WRITE_KIND,
            ],
        )
        assert not relay.relay_path(proposal.id).exists()
        spine.cancel(proposal.token)
        assert not relay.relay_path(proposal.id).exists()

    def test_an_argv_only_write_gets_no_ref(self, spine):
        """``append_body=False`` writes have no free-text slot and no body to
        excerpt — a ``--ref`` there would be a flag the CLI never asked for."""
        proposal = spine.propose(
            tool="something_else",
            session="orchestrator",
            instruction="x",
            argv_prefix=["kill", "-s", "orchestrator"],
            append_body=False,
        )
        assert "--ref" not in proposal.argv_prefix
        assert proposal.build_argv() == ["kill", "-s", "orchestrator"]
