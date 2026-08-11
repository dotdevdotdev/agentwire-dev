"""What the buddy has SENT — the write-side mirror of the delivery spool (#958).

The buddy's tool surface had nine read tools: eight looking outward at the
fleet, one at mail sent TO it — and nothing showing what it had sent. Asked
"did the code word end up in the message you sent?" it held no instrument that
could answer, scraped the recipient's terminal, and confabulated. This module
is the instrument.

**Recorded, not reconstructed.** :func:`record_write` is called from the one
place a buddy write executes — ``ConfirmSpine.confirm``, right after the
runner returns — and what it records is the executed argv itself. For a write
that carries a body, the body is ``argv[-1]``: the exact rendered string the
CLI received, never a re-render from the proposal, because ``render_body`` is
under active change (#953) and a reconstruction would quietly diverge from
what actually went out. Where the argv and the proposal disagree, the record
sides with the argv.

**Which writes have a body is the SPEC's answer, not the argv's** (#979).
``argv[-1]`` is the body of a ``msg send``; for an ``append_body=False``
write it is a session name or a flag value, and recording that as "the body"
would be a confident, specific lie — the instructions tell the model to quote
this field verbatim as the authoritative answer to "what did I send". So
:func:`record_write` reads ``proposal.append_body`` and records no body at all
where there is none, plus the flag itself so a reader can tell "no body" from
"body not recorded".

**Delivery state is computed at read time, never stored.** A message's state
changes after the write returns — queued now, delivered or dead-lettered
later — so a stored state is a lie with a timestamp. :func:`delivery_state`
asks the recipient's real inbox (the same store ``agentwire msg inbox`` and
``msg dead`` read): still pending → ``queued``; in the graveyard →
``dead_lettered`` with the drop reason.

**Neither store matching is NOT proof of delivery** (#979). This module used
to answer ``delivered`` there, reasoning that the drain removes a message from
pending only by delivering or dead-lettering it. That is false: ``msg purge``
drops the pending queue outright and ``msg dead --purge`` clears the
graveyard — both documented escape hatches, both leaving exactly this trace.
A purged message and a delivered one are indistinguishable from here, so the
state is ``no_longer_queued`` and its detail says both halves. The narrower
claim is not a hedge on the same sentence: the two stores establish that it
left the queue, and nothing about who read it. Nothing in the inbox records
per-message delivery (the ``delivered`` event carries a count and kinds, no
ids), so a positive answer would need a new mechanism, not a bolder reading of
this one. A write whose dispatch failed short-circuits to ``dispatch_failed``.

**Recording never raises.** It runs after the write has executed. An exception
here would propagate to the dispatcher's catch-all, which tells the owner
"nothing happened" about a message already sitting in the recipient's inbox —
an under-claim on the one path where the system positively knows the write
went out. A failed record is a gap in the log; a raised one is a false denial.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import delivery


def outbox_path(buddy: str) -> Path:
    """Append-only JSONL of writes *buddy* has executed, beside its spool."""
    return delivery.session_state_dir(buddy) / "outbox.jsonl"


def _flag_value(argv: list, flag: str) -> str:
    try:
        return str(argv[list(argv).index(flag) + 1])
    except (ValueError, IndexError):
        return ""


def record_write(proposal, argv: list, result: dict) -> None:
    """Record one executed write. Called post-execution; never raises."""
    try:
        argv = [str(a) for a in argv]
        dispatched = bool((result or {}).get("success", False))
        # The spec decides, not the argv shape. Absent (a caller with no such
        # attribute) means the body-carrying default, which is what every
        # shipped spec is and what ``Proposal`` itself defaults to.
        append_body = bool(getattr(proposal, "append_body", True))
        entry = {
            "proposal_id": getattr(proposal, "id", "") or "",
            "session": _flag_value(argv, "--to") or getattr(proposal, "session", ""),
            "buddy": _flag_value(argv, "--from")
            or str(getattr(proposal, "params", {}).get("_buddy") or ""),
            "kind": _flag_value(argv, "--kind"),
            "instruction": getattr(proposal, "instruction", "") or "",
            "append_body": append_body,
            "body": (argv[-1] if argv else "") if append_body else "",
            "argv": argv,
            "ts": time.time(),
            "dispatched": dispatched,
        }
        if not dispatched:
            entry["error"] = str((result or {}).get("error", ""))
        path = outbox_path(entry["buddy"] or "unknown")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
    except Exception:
        return


def read_outbox(buddy: str, limit: "int | None" = None) -> list[dict]:
    """Recorded writes for *buddy*, newest first."""
    path = outbox_path(buddy)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read().splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in raw:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    entries.reverse()
    return entries[:limit] if limit else entries


def delivery_state(entry: dict) -> dict:
    """The CURRENT state of one recorded write, from the recipient's inbox.

    Matches by exact body first, then by the ``#<proposal-id>`` tag — the tag
    survives a change in body shape (#953), so a divergence between the
    recorded body and the enqueued text degrades to a looser match rather than
    silently reading as having left the queue.
    """
    if not entry.get("dispatched", False):
        return {"state": "dispatch_failed", "detail": str(entry.get("error", ""))}

    # No body means no message, and therefore no queue to interrogate: the
    # write completed when the CLI returned. ``append_body`` is the property
    # that decides it; the empty-``kind`` test is the older proxy for the same
    # thing, kept for records written before the flag existed and for a
    # body-carrying argv that never named a kind.
    if entry.get("append_body") is False or not str(entry.get("kind") or ""):
        return {"state": "executed"}

    from .. import inbox  # deferred, matching write_tools — keeps import light

    # The WHOLE name. Stripping `@machine` here read a different session's
    # local inbox and reported its state as this message's; `inbox` keys on
    # the whole string, so this is the only question that can answer about
    # this message. Remote targets are refused upstream now (#979) — this
    # matters for records written before that ruling.
    session = str(entry.get("session") or "")
    body = str(entry.get("body") or "")
    proposal_id = str(entry.get("proposal_id") or "")
    tag = f"#{proposal_id}" if proposal_id else ""

    def matches(message) -> bool:
        text = getattr(message, "text", "") or ""
        return (body != "" and body == text) or (tag != "" and tag in text)

    try:
        if any(matches(m) for m in inbox.list_messages(session)):
            return {"state": "queued"}
        for m in inbox.list_dead(session):
            if matches(m):
                return {
                    "state": "dead_lettered",
                    "detail": getattr(m, "reason", "") or "",
                }
    except Exception as exc:
        # An unreadable inbox is not knowledge. "unknown" is the honest state;
        # guessing here would be the confabulation with extra steps.
        return {"state": "unknown", "detail": f"could not check the inbox: {exc}"}
    return {
        "state": "no_longer_queued",
        "detail": (
            f"not waiting in {session}'s inbox and not in its dead-letter store. "
            "That is what a delivered message looks like, and also what one looks "
            "like after a purge — so it is no longer queued, and whether it was "
            "read is not something this can tell you."
        ),
    }
