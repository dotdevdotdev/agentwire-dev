"""The buddy's writes: DECLARED specs, one generated propose/confirm/cancel path.

Q2 is settled as **handoff, not a tool**. The buddy does not build a
``worktree_create`` argv, does not create sessions, does not own a checkout and
does not appear in the topology. Its canonical write is
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

**Why declarations and not triples (#966).** Each write used to be three
hand-written functions with a hand-frozen ``argv_prefix``. Ten writes that way
is nine more triples and nine more chances to freeze the argv wrong — and a
mis-frozen argv is exactly how a model-supplied string reaches the CLI. A
write is now a :class:`WriteSpec`: name, params schema, a ``freeze`` function
that validates the model's arguments into a :class:`FrozenWrite`, and the
spoken templates. :func:`gated_triple` generates the propose/confirm/cancel
tools from it, so the invariants live in ONE place:

- the WHOLE argv is frozen at propose time, inside ``freeze``;
- a proposal is single-use and consumed on success (the spine);
- the approving utterance never reaches the body (#953 — the spine);
- ``render_body``'s leading-dash assertion guards every body-carrying write;
- an argv-only write (``append_body=False``) may contain NO free text — every
  element must come out of a validator, which ``freeze`` is the one place to do.

**The tier audit lives in** :mod:`~agentwire.voice_layer.surface` — which
capabilities are reads, which are writes and at what grade, and which are
excluded permanently. Read it before adding a spec here.

**On the pull toward a bootstrap escape hatch.** When nothing is live there is
an obvious, tempting fix: let the buddy start one orchestrator, just this once,
so the handoff has somewhere to go. That is session-creation semantics through
the back door and it is not built here. :func:`_require_live` refuses and the
buddy says "nothing is listening" out loud, which is a correct and useful
spoken answer (spec §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..mcp_core import run_agentwire_cmd
from .confirm import spoken_nonce, strip_controls
from .tools import ToolError, _session_arg

#: The message kind a buddy handoff carries. Already in ``ESCALATE_KINDS``, so
#: a dead-lettered buddy write emails the owner today.
WRITE_KIND = "request"

#: An instruction longer than this is not a spoken instruction — it is a
#: mis-transcription or a runaway generation. Refuse rather than hand it on.
#: Distinct from ``confirm.MAX_RENDERED_INSTRUCTION_CHARS``, which bounds what
#: reaches the recipient's pane.
MAX_INSTRUCTION_CHARS = 600


def _require_live(session: str, cannot: str) -> None:
    """Refuse a write aimed at a session that positively is not there (spec §5).

    ``inbox.live_sessions()`` returns ``None`` when tmux itself is unreachable,
    which is an outage rather than a gone target — we cannot prove anything, so
    the write proceeds and the ordinary CLI path reports what it finds. Only
    POSITIVE knowledge (tmux answered, the target is not in the list) refuses.

    *cannot* names what the buddy cannot do instead, spoken verbatim.

    The name is compared WHOLE. It used to be compared as ``split("@")[0]``,
    from when the voice pattern admitted ``name@machine``: the bare half of a
    remote name was checked against LOCAL tmux, so a remote session got either
    a false "nothing is listening" or — worse — a pass, on the strength of a
    local session that happens to share its name. Remote targets are out of
    scope (owner ruling, 2026-08-09), but nothing refuses the ``@`` SYNTAX —
    that was the first attempt at the ruling and it was itself a false
    statement, since ``ops@edge`` is a creatable, addressable LOCAL session
    (see surface.py, "Remote ``name@machine`` targets are out of scope"). What
    ships is a LIVENESS gate: ``tools._session_arg`` checks the shape, then
    refuses any whole name containing ``@`` that local tmux does not report
    live. So a name reaching here is local by DEMONSTRATION — and the bare half
    before the ``@`` is exactly the string that was never the thing
    demonstrated, which is why splitting it could only re-open that gap. The
    one case that demonstrates nothing is an unreachable tmux, where both that
    gate and this one refuse nothing (spec §5) rather than either guessing.
    """
    from .. import inbox

    live = inbox.live_sessions()
    if live is None:
        return
    if session not in live:
        raise ToolError(
            f"Nothing is listening — there's no live session called '{session}'. "
            f"Check what's actually running and say the name again. {cannot}"
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


def _buddy_arg(args: dict) -> str:
    buddy = args.get("_buddy") or ""
    if not buddy:
        raise ToolError("buddy identity missing from tool context")
    return buddy


@dataclass(frozen=True)
class FrozenWrite:
    """The output of a spec's ``freeze``: everything that will run, validated.

    ``argv_prefix`` is complete and final. When ``append_body`` is True the
    spine appends the §4b rendered body (instruction + request utterance +
    proposal id) at execution; when False, the prefix IS the argv, so every
    element must already have passed a validator — there is no free-text slot.
    """

    session: str
    instruction: str
    argv_prefix: tuple[str, ...]
    append_body: bool = True
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WriteSpec:
    """One gated write, as a declaration.

    ``announce_template`` may use ``{session}``, ``{instruction}`` and
    ``{phrase}``. ``fallback_template`` gets ONLY ``{session}`` and
    ``{instruction}`` — the browser-voice fallback is not echo-cancelled, so
    the nonce must be structurally unreachable from it (see the announce
    payload below); a template asking for ``{phrase}`` fails at import.

    ``success_say`` empty means the write QUEUES (the msg default and its
    "queued" claim); non-empty means the write completes when the runner
    returns, and this is what the buddy says. §3.6 both ways: never claim more
    than the write did, never less.
    """

    name: str
    action: str  # short verb phrase, e.g. "passing a message to a running session"
    params_schema: dict
    freeze: Callable[[dict], FrozenWrite]
    announce_template: str
    fallback_template: str
    success_say: str = ""

    def __post_init__(self):
        if "{phrase}" in self.fallback_template or "{nonce}" in self.fallback_template:
            raise ValueError(
                f"WriteSpec {self.name}: the fallback template must not carry "
                "the confirm phrase — the browser-voice channel can echo into "
                "the approval window."
            )


def gated_triple(spec: WriteSpec) -> tuple:
    """Generate the ``propose_/send_/cancel_<name>`` tool entries for *spec*.

    The propose payload's shape is load-bearing and shared: ``say`` is LITERAL
    TEXT TO UTTER (#950 — a directive here gets read aloud verbatim),
    ``fallback_say`` is the echo-safe channel and never carries the nonce, and
    ``anchor_proposal_id`` is what the client anchors to the spoken turn.
    """

    def propose(args: dict, spine, _spec=spec) -> dict:
        frozen = _spec.freeze(args)
        proposal = spine.propose(
            tool=f"send_{_spec.name}",
            session=frozen.session,
            instruction=frozen.instruction,
            argv_prefix=list(frozen.argv_prefix),
            # ``_buddy`` rides on EVERY proposal, not just the specs whose argv
            # happens to carry ``--from``. It is how the outbox attributes an
            # executed write (#979): a write filed under "unknown" is one the
            # buddy's own ``buddy_sent`` cannot see, which is the instrument
            # failing at exactly the question it was built for. A spec's own
            # params win — freeze is the authority on what it validated.
            params={
                "session": frozen.session,
                "_buddy": args.get("_buddy") or "",
                **frozen.params,
            },
            append_body=frozen.append_body,
            success_say=_spec.success_say,
        )
        phrase = f"confirm {spoken_nonce(proposal.nonce)}"
        slots = {"session": frozen.session, "instruction": frozen.instruction}
        return {
            "success": True,
            "confirm_token": proposal.token,
            "proposal_id": proposal.id,
            "session": frozen.session,
            "message": frozen.instruction,
            "confirm_phrase": phrase,
            "needs_spoken_approval": True,
            "must_speak": True,
            "anchor_proposal_id": proposal.id,
            "say": _spec.announce_template.format(phrase=phrase, **slots),
            "fallback_say": _spec.fallback_template.format(**slots),
            "model_guidance": (
                "Say the code word clearly, as a word; do not spell it out. "
                f"Do not call send_{_spec.name} until you have said the "
                "proposal aloud and the owner has answered."
            ),
        }

    def send(args: dict, spine, _spec=spec) -> dict:
        token = args.get("confirm_token")
        if not isinstance(token, str) or not token.strip():
            raise ToolError(
                "I need the confirm token from the proposal. Propose it first."
            )
        return spine.confirm(token.strip()).to_dict()

    def cancel(args: dict, spine, _spec=spec) -> dict:
        token = args.get("confirm_token")
        if not isinstance(token, str) or not token.strip():
            raise ToolError("I need the confirm token of the proposal being cancelled.")
        return spine.cancel(token.strip()).to_dict()

    token_schema = {
        "type": "object",
        "properties": {
            "confirm_token": {
                "type": "string",
                "description": f"The confirm_token from propose_{spec.name}.",
            },
        },
        "required": ["confirm_token"],
        "additionalProperties": False,
    }
    return (
        (
            f"propose_{spec.name}",
            (
                f"STEP ONE of {spec.action}. Prepares it and returns a confirm "
                "phrase. This does NOTHING yet. After calling it you must say "
                "out loud what you are about to do and the exact confirm phrase "
                "the owner has to speak."
            ),
            spec.params_schema,
            propose,
        ),
        (
            f"send_{spec.name}",
            (
                f"STEP TWO of {spec.action}. Executes it, but only if the owner "
                "spoke the exact confirm phrase after you said it. That is "
                "checked in code against the transcript, independently of you — "
                "calling this on a 'yeah' or on your own impression will be "
                "refused and will tell you why. Takes only the confirm_token."
            ),
            token_schema,
            send,
        ),
        (
            f"cancel_{spec.name}",
            (
                f"Drop a proposal for {spec.action} the owner declined or "
                "changed their mind about. Does nothing and never fails."
            ),
            token_schema,
            cancel,
        ),
    )


# ---------------------------------------------------------------------------
# The declared writes
# ---------------------------------------------------------------------------


def _freeze_session_message(args: dict) -> FrozenWrite:
    session = _session_arg(args)
    instruction = _instruction_arg(args)
    buddy = _buddy_arg(args)
    _require_live(
        session,
        cannot="I can't start a session; that has to be a real orchestrator.",
    )
    return FrozenWrite(
        session=session,
        instruction=instruction,
        # Frozen here, at propose time — the WHOLE argv. The body's said: slot
        # carries the owner's request utterance captured by spine.propose from
        # the transcript ring, never the approving utterance (#953).
        argv_prefix=(
            "msg", "send", "--to", session, "--from", buddy, "--kind", WRITE_KIND,
        ),
        append_body=True,
        params={"message": instruction, "_buddy": buddy},
    )


SESSION_MESSAGE_SPEC = WriteSpec(
    name="session_message",
    action="passing a message to a running session",
    params_schema={
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
    freeze=_freeze_session_message,
    announce_template=(
        "I'm ready to send to {session}: '{instruction}'. To approve, say {phrase}."
    ),
    # What the BROWSER-VOICE fallback speaks instead of `say`. speechSynthesis
    # output is not on the WebRTC path, so echo cancellation does not suppress
    # it — anything this channel utters can re-enter the microphone and land in
    # the USER transcript inside the approval window. So the nonce is not
    # reachable from here, structurally (WriteSpec.__post_init__ enforces it).
    fallback_template=(
        "I'm ready to send to {session}: '{instruction}'. "
        "Ask me for the code word when you want me to send it."
    ),
)

#: Every declared gated write. Adding one is adding a spec here — the triple,
#: the freeze discipline and the spoken payloads are generated.
WRITE_SPECS: tuple[WriteSpec, ...] = (SESSION_MESSAGE_SPEC,)

WRITE_TOOL_SPECS = tuple(
    entry for spec in WRITE_SPECS for entry in gated_triple(spec)
)

#: name → callable, for callers (and tests) that address a generated tool
#: directly rather than through ``tools.dispatch``.
WRITE_TOOL_FNS = {name: fn for name, _desc, _schema, fn in WRITE_TOOL_SPECS}

# The generated callables are this module's public functions — a caller may
# say ``write_tools.propose_session_message`` without knowing the registry.
globals().update(WRITE_TOOL_FNS)


def dispatch_write(argv: list[str]) -> dict:
    """The runner ``ConfirmSpine`` calls on approval. One place, one command."""
    return run_agentwire_cmd(argv)
