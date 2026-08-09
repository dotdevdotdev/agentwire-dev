"""What the buddy has SENT — the write-side mirror of the delivery spool (#958).

The buddy's tool surface had nine read tools: eight looking outward at the
fleet, one at mail sent TO it — and nothing showing what it had sent. Asked
"did the code word end up in the message you sent?" it held no instrument that
could answer, scraped the recipient's terminal, and confabulated. This module
is the instrument.

**Recorded, not reconstructed.** :func:`record_write` is called from the one
place a buddy write executes — ``ConfirmSpine.confirm``, right after the
runner returns — and what it records is the executed argv itself. The body is
``argv[-1]``, the exact rendered string the CLI received, never a re-render
from the proposal: ``render_body`` is under active change (#953), and a
reconstruction would quietly diverge from what actually went out, which is the
precise failure this exists to end. Where the argv and the proposal disagree,
the record sides with the argv.

**Delivery state is computed at read time, never stored.** A message's state
changes after the write returns — queued now, delivered or dead-lettered
later — so a stored state is a lie with a timestamp. :func:`delivery_state`
asks the recipient's real inbox (the same store ``agentwire msg inbox`` and
``msg dead`` read): still pending → ``queued``; in the graveyard →
``dead_lettered`` with the drop reason; neither → ``delivered``, because the
drain removes a message from pending only by delivering it or dead-lettering
it. A write whose dispatch failed short-circuits to ``dispatch_failed``.

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
        entry = {
            "proposal_id": getattr(proposal, "id", "") or "",
            "session": _flag_value(argv, "--to") or getattr(proposal, "session", ""),
            "buddy": _flag_value(argv, "--from"),
            "kind": _flag_value(argv, "--kind"),
            "instruction": getattr(proposal, "instruction", "") or "",
            "body": argv[-1] if argv else "",
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
    silently reading as ``delivered``.
    """
    if not entry.get("dispatched", False):
        return {"state": "dispatch_failed", "detail": str(entry.get("error", ""))}

    from .. import inbox  # deferred, matching write_tools — keeps import light

    session = str(entry.get("session") or "").split("@")[0]
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
        # guessing "delivered" here would be the confabulation with extra steps.
        return {"state": "unknown", "detail": f"could not check the inbox: {exc}"}
    return {"state": "delivered"}
