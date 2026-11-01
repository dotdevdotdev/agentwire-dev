"""The sender-side ledger — what became of the messages I sent (#1231).

The inbox is keyed by RECIPIENT and the drain unlinks a file the moment it
delivers, so once a message left the queue "delivered" and "purged" were
indistinguishable and a sender had no way to ask. This module is the ONE
writer and the ONE reader of a per-sender record that outlives the file:

    ~/.agentwire/sent-ledger/<quoted-sender>.json   {id: record, ...}

* **Writers** — ``inbox.enqueue`` (``queued``) and every terminal transition in
  the inbox: the drain's delivery sites via ``inbox._note_delivered``,
  dead-letter in ``_bump_attempts`` / ``gc_sender``, ``purge_pending``,
  ``pull_ingest``. Nothing else touches the file.
* **Readers** — ``agentwire msg sent`` and MCP ``msg_sent``, both through
  :func:`list_sent`. Neither re-derives state.

**What "delivered" means:** the text was pasted into the recipient's input
box. Never "read", never "acted on". ``verified`` says how strong the evidence
of the paste is (see :func:`record_delivered`); the output must not imply more.

**Reconciliation, not trust.** The ledger can miss a transition (a crash
between unlink and write, a delivery site added later without a hook). A
record still saying ``queued`` is therefore checked against the live inbox at
read time: file present → ``queued`` with the current deferral reason; file
in ``dead/`` → ``dead_lettered`` (healed from the corpse); file gone with no
recorded outcome → ``unknown``, with the reason stated. A missed hook degrades
to ``unknown``, never to a confident wrong answer.

**Bounded.** At most :data:`MAX_ENTRIES` per sender, nothing older than
:data:`TTL_MS`; both enforced on every write.

**Identity.** A ledger is read only by the session that owns it. The CLI/MCP
resolve the caller from tmux and take no session flag; there is deliberately
no cross-session read (a sender's bodies are theirs to see, not the fleet's).
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from . import inbox

MAX_ENTRIES = 500
TTL_MS = 7 * 24 * 60 * 60 * 1000  # a week: long enough to audit a run, short enough to bound

STATES = ("queued", "delivered", "dead_lettered", "purged", "pulled", "unknown")
_TERMINAL = frozenset({"delivered", "dead_lettered", "purged", "pulled"})

# Senders that are not a session: no ledger (nobody could ever read it).
_NO_LEDGER = frozenset({"", "unknown"})

# The paste-evidence tiers a delivery can carry. ``verified`` is True when the
# drain POSITIVELY observed the text (box-cleared confirm, scrollback echo, a
# confirmed Enter-only heal). False means the drain consumed the message on
# weaker grounds.
HOW_PASTED = "pasted"  # safe_deliver confirmed the box cleared
HOW_DEDUP = "scrollback"  # the render was found on the recipient's scrollback
HOW_HEALED = "healed"  # our paste sat in the box; Enter-only heal confirmed
HOW_LATE = "late"  # exhaustion verdict found delivery evidence


def ledger_dir() -> Path:
    """Beside the inbox, resolved at call time so a patched ``INBOX_ROOT``
    (the test seam) carries the ledger with it."""
    return inbox.INBOX_ROOT.parent / "sent-ledger"


def _path(sender: str) -> Path:
    # quote(safe="") is injective: worktree sessions ("proj/branch") get a flat,
    # collision-free filename and no path component can escape the directory.
    return ledger_dir() / f"{quote(sender, safe='')}.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


@contextmanager
def _locked(sender: str) -> Iterator[dict]:
    """Read-modify-write one sender's ledger under an flock.

    Writers are cross-recipient (a drain for A and a drain for B can both
    finish a message from the same sender), so the per-recipient drain lock
    does not cover this file. The caller mutates the yielded dict in place;
    it is pruned and written back atomically on exit.
    """
    path = _path(sender)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            entries = _load(path)
            yield entries
            _prune(entries)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(entries))
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _prune(entries: dict) -> None:
    cutoff = _now_ms() - TTL_MS
    for mid in [m for m, r in entries.items() if r.get("state_ts", r.get("ts", 0)) < cutoff]:
        del entries[mid]
    overflow = len(entries) - MAX_ENTRIES
    if overflow > 0:
        for mid in sorted(entries, key=lambda m: (entries[m].get("ts", 0), m))[:overflow]:
            del entries[mid]


def _new_record(msg: "inbox.Message") -> dict:
    return {
        "id": msg.id, "to": msg.to, "kind": msg.kind, "text": msg.text, "ts": msg.ts,
        "state": "queued", "state_ts": msg.ts,
    }


def record_sent(messages: "list[inbox.Message]") -> None:
    """Open a ``queued`` record for each message just enqueued."""
    for sender, group in _by_sender(messages).items():
        with _locked(sender) as entries:
            for msg in group:
                entries.setdefault(msg.id, _new_record(msg))


def _by_sender(messages: "list[inbox.Message]") -> "dict[str, list[inbox.Message]]":
    grouped: dict[str, list[inbox.Message]] = {}
    for msg in messages:
        if msg.sender not in _NO_LEDGER:
            grouped.setdefault(msg.sender, []).append(msg)
    return grouped


def _transition(messages: "list[inbox.Message]", state: str, **fields) -> None:
    """Move each message to a terminal *state*. First terminal state wins: a
    record that already ended is never rewritten (a late sweep must not turn a
    ``delivered`` into a ``purged``). A message with no record yet (sent before
    this ledger existed) gets one built from the message in hand."""
    now = _now_ms()
    for sender, group in _by_sender(messages).items():
        with _locked(sender) as entries:
            for msg in group:
                rec = entries.setdefault(msg.id, _new_record(msg))
                if rec.get("state") in _TERMINAL:
                    continue
                rec.update(state=state, state_ts=now, **fields)


def record_delivered(
    messages: "list[inbox.Message]", how: str = HOW_PASTED, verified: bool = True
) -> None:
    """The drain confirmed the paste. *how* names the evidence; *verified* is
    whether the drain positively observed the text (see the ``HOW_*`` tiers)."""
    _transition(messages, "delivered", how=how, verified=verified)


def record_dead(messages: "list[inbox.Message]") -> None:
    """Dead-lettered; each message carries its own drop reason (``msg.reason``)."""
    now = _now_ms()
    for sender, group in _by_sender(messages).items():
        with _locked(sender) as entries:
            for msg in group:
                rec = entries.setdefault(msg.id, _new_record(msg))
                if rec.get("state") not in _TERMINAL:
                    rec.update(state="dead_lettered", state_ts=msg.dead_ts or now, reason=msg.reason)


def record_purged(messages: "list[inbox.Message]", why: str) -> None:
    _transition(messages, "purged", reason=why)


def record_pulled(messages: "list[inbox.Message]") -> None:
    _transition(messages, "pulled")


# ── Reading ───────────────────────────────────────────────────────────────


def _locate(rec: dict) -> "tuple[str, inbox.Message | None]":
    """Where the message is NOW: ``("queue"|"passive"|"dead"|"gone", msg)``."""
    to, name = rec.get("to", ""), f"{rec['id']}.json"
    try:
        base = inbox.session_dir(to)
    except ValueError:
        return "gone", None
    for where, path in (
        ("queue", base / name), ("passive", base / "ingest" / name),
        ("dead", base / "dead" / name),
    ):
        if path.exists():
            return where, inbox._read_message(path)
    return "gone", None


def _view(rec: dict) -> dict:
    """The reader's view of one record: stored state, reconciled with reality."""
    out = {
        "id": rec["id"], "to": rec.get("to", ""), "kind": rec.get("kind", ""),
        "text": rec.get("text", ""), "sent_ts": rec.get("ts", 0),
        "state": rec.get("state", "unknown"), "state_ts": rec.get("state_ts", 0),
    }
    if out["state"] == "queued":
        where, msg = _locate(rec)
        if where == "queue" and msg is not None:
            out.update(reason=msg.reason, attempts=msg.attempts, reason_ts=msg.reason_ts)
        elif where == "passive":
            out.update(reason="passive: waits for the recipient to `msg pull` it")
        elif where == "dead" and msg is not None:
            out.update(
                state="dead_lettered", state_ts=msg.dead_ts, reason=msg.reason,
                attempts=msg.attempts,
            )
        else:
            out.update(
                state="unknown",
                reason="left the recipient's inbox with no recorded outcome "
                       "(delivered, purged or dead-lettered and purged — the ledger "
                       "missed the transition)",
            )
        return out
    for key in ("reason", "how", "verified"):
        if key in rec:
            out[key] = rec[key]
    return out


def list_sent(
    sender: str, limit: int = 20, to: "str | None" = None, message_id: "str | None" = None
) -> dict:
    """The calling session's sent messages, newest first.

    *message_id* matches the full id or the 6-char tail shown on a delivered
    line (``⟨#id6⟩``). Returns ``{"sender", "total", "sent": [view, …]}`` —
    ``total`` is the count AFTER filtering and BEFORE *limit*.
    """
    entries = _load(_path(sender)) if sender not in _NO_LEDGER else {}
    # (ts, id): `ts` is ms, so a burst inside one millisecond ties; the id's
    # nanosecond prefix is the true send order.
    rows = sorted(entries.values(), key=lambda r: (r.get("ts", 0), r["id"]), reverse=True)
    if to:
        rows = [r for r in rows if r.get("to") == to]
    if message_id:
        rows = [r for r in rows if r["id"] == message_id or r["id"].endswith(f"-{message_id}")]
    return {
        "sender": sender, "total": len(rows),
        "sent": [_view(r) for r in rows[: max(limit, 0)]],
    }


def describe(view: dict) -> str:
    """One line: state plus whatever qualifies it (reason, evidence)."""
    state = view["state"]
    parts = [state]
    if view.get("reason"):
        parts.append(view["reason"])
    if state == "delivered":
        parts.append(
            "paste confirmed" if view.get("verified") else "paste NOT independently confirmed"
        )
    if state == "queued" and view.get("attempts"):
        parts.append(f"{view['attempts']} attempts")
    return " · ".join(parts)
