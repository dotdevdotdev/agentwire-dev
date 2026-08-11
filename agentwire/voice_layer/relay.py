"""The FULL relayed utterance, on disk, because the pane cannot carry it (#1015).

The §4b body is capped hard — ``MAX_BODY_CHARS`` and its 160-char instruction
slot are not tidiness, they are the measured boundary past which the #689 heal
stops firing and a swallowed Enter wedges the message permanently. So a long
spoken request had exactly two possible fates and the shipped one was the bad
one: the recipient got ``"Treat it as a running list for anyt…"`` and acted on
the half it could see. Every relay in the first live voice session arrived that
way.

**A cap you cannot raise is not an argument for losing the text.** The pane is
one channel; the filesystem is another, and the recipient is an agent that
reads files. So the whole utterance is written here and the body carries a
pointer to it — the same shape ``ingest`` messages already use (``--ref``),
with the inline excerpt demoted to what it always actually was: a preview.

Three properties, each with a named failure:

- **Writing never raises.** :func:`write_relay` is called from
  ``Proposal.build_argv()``, which ``ConfirmSpine._dispatch`` runs *after*
  ``_proposals.pop()`` and *outside* the runner's ``try`` — a raise there eats
  the proposal with the approving utterance already spent, and the owner is not
  watching a screen (the ``_lead_safe`` lesson, same position, same cost). A
  failed write returns ``""`` and the message goes out as it does today:
  excerpt only, and the caller drops the ``--ref`` too, because a pointer to a
  file that is not there is worse than no pointer.
- **The pointer and the file agree.** The path is derived from the proposal id
  (:func:`relay_path`), frozen into the argv at propose time, and the body's
  ``full:`` slot is rendered from the path the write actually returned — never
  from a second construction of it.
- **The store is bounded.** Relays are small and permanent-by-default is a slow
  leak, so each write prunes entries older than :data:`RETENTION_DAYS`. Pruning
  is best-effort and never raises for the same reason writing does not.

The id in the filename is ``secrets.token_hex(3)`` from ``ConfirmSpine.propose``
— six hex characters, so the path is fixed-length and traversal-free by
construction rather than by validation. :func:`relay_path` asserts that shape
anyway, since it is what makes the sentence above true.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .. import core

#: How long a relay file survives. Long enough that a session picking up a
#: message hours later still finds it, short enough that the directory does not
#: grow without bound.
RETENTION_DAYS = 30

#: The proposal-id shape ``ConfirmSpine.propose`` mints. Anything else is a
#: caller bug, not a path to sanitise.
_ID_RE = re.compile(r"^[0-9a-f]{4,32}$")


def relay_dir() -> Path:
    """``~/.agentwire/voice/relays/`` — a FUNCTION, not a constant.

    An import-time constant silently ignores a patched ``core.CONFIG_DIR``,
    which is how a test seam turns into a write against the owner's real config
    directory (the ``role_prompts_dir`` lesson, #871).
    """
    return core.CONFIG_DIR / "voice" / "relays"


def relay_path(proposal_id: str) -> Path:
    """Where the relay for *proposal_id* lives. Deterministic, so the argv can
    carry the path before the file exists."""
    if not _ID_RE.match(proposal_id or ""):
        raise ValueError(f"not a proposal id: {proposal_id!r}")
    return relay_dir() / f"{proposal_id}.md"


def _prune(now: float) -> None:
    cutoff = now - RETENTION_DAYS * 86400
    try:
        entries = list(relay_dir().glob("*.md"))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def render_relay(
    *,
    proposal_id: str,
    session: str,
    sender: str,
    instruction: str,
    request_utterance: str,
    when: float,
) -> str:
    """The file's contents: both fields entire, neither clipped.

    Nothing here is clipped — that is the entire point of the file. It is
    Markdown because the reader is an agent with a Read tool, and it names the
    proposal id so a reader holding the delivered line can tell it is looking at
    the same write rather than at a neighbouring one.

    **The quote is ONE transcript entry, and the heading says so.** *instruction*
    is the whole request as the buddy understood it, but *request_utterance*
    comes from ``request_utterance_from``, which returns a single ring entry —
    so a spoken request delivered across several segments reproduces only the
    segment that carried the request. Narrowed rather than qualified: calling
    this "what the owner said" would be a broader claim than the ring can
    support, and a broad claim is what gets rounded back up later.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(when))
    lines = [
        f"# Voice relay #{proposal_id}",
        "",
        f"- **to:** {session}",
        f"- **from:** {sender or 'buddy'}",
        f"- **when:** {stamp}",
        "",
        "## Message (full)",
        "",
        instruction.strip(),
    ]
    if request_utterance.strip():
        lines += [
            "",
            "## The request utterance, verbatim (one spoken segment)",
            "",
            f"> {request_utterance.strip()}",
        ]
    lines += [
        "",
        "---",
        "",
        "The delivered message carried only an excerpt of the above — the pane "
        "has a measured size limit. This file is the whole request; act on "
        "this, not on the excerpt.",
        "",
    ]
    return "\n".join(lines)


def write_relay(
    path: Path,
    *,
    proposal_id: str,
    session: str,
    sender: str,
    instruction: str,
    request_utterance: str,
) -> str:
    """Persist the full relay at *path*. Returns the path as a string, or ``""``.

    **Never raises**, deliberately — see the module docstring. Every failure
    mode (unwritable directory, full disk, a patched CONFIG_DIR pointing
    somewhere impossible) degrades to today's behaviour rather than destroying
    a message the owner has already approved.

    Written through :func:`core.write_owner_only` — 0600, mode set on the
    descriptor before any bytes land, ``os.replace`` for atomicity. Not a
    hand-rolled temp-and-replace: this file holds the owner's verbatim speech,
    which is exactly the content class #887 tightened, and that function is
    documented as the ONE implementation *because* the third hand-rolled copy is
    how a 0644 file carrying private content reached the wild. It also owns the
    temp file's lifetime, where a fixed ``.md.tmp`` name would leave an orphan
    that :func:`_prune`'s ``*.md`` glob can never collect.

    Atomicity is load-bearing rather than tidy: a partial relay read by the
    recipient is a *silently* partial instruction, which is the defect this
    module exists to remove, not a variant of it worth shipping.
    """
    now = time.time()
    body = render_relay(
        proposal_id=proposal_id,
        session=session,
        sender=sender,
        instruction=instruction,
        request_utterance=request_utterance,
        when=now,
    )
    try:
        core.write_owner_only(path, body)
    except Exception:
        return ""
    try:
        _prune(now)
    except Exception:
        pass
    return str(path)
