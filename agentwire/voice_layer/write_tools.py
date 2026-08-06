"""The buddy's ONE write: a message to a session that already has guards (spike).

Q2 is settled as **handoff, not a tool**. The buddy does not build a
``worktree_create`` argv, does not create sessions, does not own a checkout and
does not appear in the topology. Its only write is
``agentwire msg send --to <session> --from <buddy> --kind request <body>``,
addressed to a real Claude session which DOES have damage-control hooks,
posture, worktree isolation and prompt routing.

That distinction is the precise one, and the loose version contradicts itself:
**the sending is unguarded; the acting-on-it is guarded.** No programmatic path
to ``msg send`` has damage-control coverage — not this ``subprocess.run``, and
not ``mcp__agentwire__msg_send`` either, which is absent from the ``PreToolUse``
matcher list. What the RECIPIENT does with the message runs inside a real Claude
session with hooks. That is the whole point of handoff, and it is why the
boundary holds by construction rather than by discipline.

T1 ("spawn a worker") therefore adds no new authority when it lands: under
handoff it is the same write with different words in the body. If it ever looks
like it needs a tool that creates a session, the boundary has moved and the diff
is wrong.

**Why ``--kind request`` and not a new ``voice`` kind.** A ``voice`` kind is
deferred to Slice 1b on purpose. It is the only part of this work that changes
behaviour for sessions with nothing to do with voice, it touches a shared
subsystem's escalation and dead-letter paths, and its blast radius is
non-obvious — the adversarial review found two hand-written kind tuples
(``doctor_cli``, ``session_cli``) that would silently stop reporting a
dead-lettered buddy message, which is exactly the property the kind was being
argued for. ``request`` is ALREADY in ``inbox.ESCALATE_KINDS``, so the
dead-letter-emails-the-owner behaviour is achieved today, and prefix-level
attribution comes from the §4b body. The split costs nothing functionally and
buys the shared subsystem its own review.

**On the pull toward a bootstrap escape hatch.** When nothing is live there is
an obvious, tempting fix: let the buddy start one orchestrator, just this once,
so the handoff has somewhere to go. That is session-creation semantics through
the back door and it is not built here. :func:`_live_recipient_check` refuses
and the buddy says "nothing is listening" out loud, which is a correct and
useful spoken answer (spec §5).
"""

from __future__ import annotations

from ..mcp_core import run_agentwire_cmd
from .confirm import spoken_nonce, strip_controls
from .tools import ToolError, _session_arg

#: The message kind a buddy write carries. See the module docstring: already in
#: ``ESCALATE_KINDS``, so a dead-lettered buddy write emails the owner today.
WRITE_KIND = "request"

#: An instruction longer than this is not a spoken instruction — it is a
#: mis-transcription or a runaway generation. Refuse rather than hand it on.
#: Distinct from ``confirm.MAX_RENDERED_INSTRUCTION_CHARS``, which bounds what
#: reaches the recipient's pane.
MAX_INSTRUCTION_CHARS = 600


def _live_recipient_check(session: str) -> None:
    """Refuse a handoff to a session that positively is not there (spec §5).

    ``inbox.live_sessions()`` returns ``None`` when tmux itself is unreachable,
    which is an outage rather than a gone recipient — we cannot prove anything,
    so the ordinary ``msg send`` path handles it. Only POSITIVE knowledge (tmux
    answered, the target is not in the list) refuses.
    """
    from .. import inbox

    live = inbox.live_sessions()
    if live is None:
        return
    if session.split("@")[0] not in live:
        raise ToolError(
            f"Nothing is listening — there's no live session called '{session}'. "
            "Check what's actually running and say the name again. I can't start "
            "a session; that has to be a real orchestrator."
        )


def _instruction_arg(args: dict) -> str:
    value = args.get("message")
    if not isinstance(value, str) or not value.strip():
        raise ToolError("I need the message to pass on. Say what you want sent.")
    # Stripped HERE, before the freeze, as well as in render_body. The
    # realistic carrier of a control character is this field — it is
    # model-supplied and was only length-bounded — and stripping at propose
    # keeps the frozen argv clean by construction, so "frozen at propose" still
    # means what it claims rather than "frozen, then sanitised later".
    text = strip_controls(value).strip()
    if len(text) > MAX_INSTRUCTION_CHARS:
        raise ToolError(
            "That message is far longer than anything said aloud, so I probably "
            "mis-heard it. Say the short version and I'll pass that on."
        )
    return text


def propose_session_message(args: dict, spine) -> dict:
    """Mint a proposal and its nonce. Writes nothing."""
    session = _session_arg(args)
    instruction = _instruction_arg(args)
    buddy = args.get("_buddy") or ""
    if not buddy:
        raise ToolError("buddy identity missing from tool context")
    _live_recipient_check(session)

    proposal = spine.propose(
        tool="send_session_message",
        session=session,
        instruction=instruction,
        # Frozen here, at propose time. The body is the LAST element and is the
        # only thing completed at confirm — from the transcript ring and the
        # proposal id, never from anything a model says. See confirm.Proposal.
        argv_prefix=[
            "msg", "send", "--to", session, "--from", buddy, "--kind", WRITE_KIND,
        ],
        params={"session": session, "message": instruction},
    )
    phrase = f"confirm {spoken_nonce(proposal.nonce)}"
    return {
        "success": True,
        "confirm_token": proposal.token,
        "proposal_id": proposal.id,
        "session": session,
        "message": instruction,
        "confirm_phrase": phrase,
        "needs_spoken_approval": True,
        "must_speak": True,
        # The client anchors the proposal to the response.done of the turn that
        # speaks this, which is what makes "the approval postdates the proposal"
        # mean "after the owner heard it".
        "anchor_proposal_id": proposal.id,
        # Scripted text, so its CONTENT is the mechanism. A stale word here is
        # not cosmetic — it is the scripted-instructions mechanism working
        # exactly as designed, with the wrong script. (This string carried
        # "say the two digits separately" from the digit-nonce design long
        # after the alphabet became words, because it lives in a prompt string
        # rather than in logic any test exercised.)
        "say": (
            f"Tell the owner plainly what you are about to send — the actual words, "
            f"'{instruction}', and that it is going to {session} — then say: "
            f"to approve, say {phrase}. Say the code word clearly, as a word; "
            f"do not spell it out. Do not call send_session_message until you "
            f"have said this and they have answered."
        ),
    }


def send_session_message(args: dict, spine) -> dict:
    """Confirm a proposal and, if the owner spoke the nonce, hand it off.

    Takes ONE argument. There is deliberately no session or message parameter:
    everything determining what runs was frozen at propose time.
    """
    token = args.get("confirm_token")
    if not isinstance(token, str) or not token.strip():
        raise ToolError(
            "I need the confirm token from the proposal. Propose the message first."
        )
    return spine.confirm(token.strip()).to_dict()


def cancel_session_message(args: dict, spine) -> dict:
    """Retire a proposal the owner declined. Never writes."""
    token = args.get("confirm_token")
    if not isinstance(token, str) or not token.strip():
        raise ToolError("I need the confirm token of the message being cancelled.")
    return spine.cancel(token.strip()).to_dict()


WRITE_TOOL_SPECS = (
    (
        "propose_session_message",
        (
            "STEP ONE of passing a message to a running session. Prepares it and "
            "returns a confirm phrase. This sends NOTHING. After calling it you "
            "must say out loud what you are about to send, to whom, and the exact "
            "confirm phrase the owner has to speak."
        ),
        {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": (
                        "Exact session name from fleet_sessions. Never a name you "
                        "half-heard — read it back and ask instead."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "What to pass on, in the owner's own terms.",
                },
            },
            "required": ["session", "message"],
            "additionalProperties": False,
        },
        propose_session_message,
    ),
    (
        "send_session_message",
        (
            "STEP TWO. Hands off the proposed message, but only if the owner spoke "
            "the exact confirm phrase after you said it. That is checked in code "
            "against the transcript, independently of you — calling this on a "
            "'yeah' or on your own impression will be refused and will tell you "
            "why. Takes only the confirm_token."
        ),
        {
            "type": "object",
            "properties": {
                "confirm_token": {
                    "type": "string",
                    "description": "The confirm_token from propose_session_message.",
                },
            },
            "required": ["confirm_token"],
            "additionalProperties": False,
        },
        send_session_message,
    ),
    (
        "cancel_session_message",
        (
            "Drop a proposed message the owner declined or changed their mind "
            "about. Sends nothing and never fails."
        ),
        {
            "type": "object",
            "properties": {
                "confirm_token": {
                    "type": "string",
                    "description": "The confirm_token of the proposal to drop.",
                },
            },
            "required": ["confirm_token"],
            "additionalProperties": False,
        },
        cancel_session_message,
    ),
)


def dispatch_msg_send(argv: list[str]) -> dict:
    """The runner ``ConfirmSpine`` calls on approval. One place, one command."""
    return run_agentwire_cmd(argv)
