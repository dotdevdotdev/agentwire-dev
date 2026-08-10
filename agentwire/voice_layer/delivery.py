"""Inbox delivery for sessions that have no tmux pane to paste into (spike).

``inbox.flush_session`` is built around one assumption that holds for every
session agentwire has ever had: the recipient is a tmux session, so delivery
means *pasting into pane 0's input box*. Every gate in the drain encodes that
assumption — the gone gate (``live_sessions()`` is a tmux session list), the
empty-box gate, the stuck-in-box heal, and ``safe_deliver`` itself.

The voice buddy breaks the assumption: it is a real recipient with a real
identity, but its "input box" is a live audio conversation. Left alone, the
drain misreads it as a recipient that positively doesn't exist — tmux is
reachable and the buddy isn't in the list — so every ``msg send --kind done``
addressed to it dead-letters in ~5 ticks (``GONE_MAX_ATTEMPTS``).

Rather than fork the inbox (the explicit non-goal), this module is the seam:
:func:`adapter_for` answers "does this recipient want non-tmux delivery?" and
``flush_session`` consults it once, immediately after the cohort hold and
BEFORE the gone gate. Ordering matters in both directions:

- **After the cohort hold** — a report from a child the recipient is still
  waiting on belongs to ``agentwire wait --children``, which reads it straight
  off disk. Spooling it first would consume it out from under that collection.
- **Before the gone gate** — that gate is the one that would kill the message.

Delivery here means *handed to the buddy's spool*, an append-only JSONL file the
voice layer reads. Nothing pushes into the conversation from THIS side: the drain
appends and stops, which is what keeps this module out of the interrupt question
entirely. The pull is no longer only on demand, though — ``client.py`` polls the
spool on its own clock and volunteers at a gap (#962), and escalation-kind mail
rides a relaxed gate (#967). "This slice never interrupts" was true of the seam
and stopped being true of the layer. See ``docs/wiki/voice-layer.md``.

Registration is data, not code: a session opts in by carrying ``delivery``
in its ``metadata.json`` (the #871 SSOT store). No existing session has that
key, so this module is inert for every session that exists today.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import core

#: Metadata key naming the delivery adapter a session wants.
DELIVERY_KEY = "delivery"

#: The adapter the voice buddy registers under.
VOICE_ADAPTER = "voice"

#: Every adapter name the drain will honor. An unknown value in metadata falls
#: through to the ordinary tmux path rather than silently swallowing messages —
#: a typo must not become a black hole.
ADAPTERS = (VOICE_ADAPTER,)


def session_state_dir(session: str) -> Path:
    """The session's metadata directory (``~/.agentwire/sessions/<name>/``).

    Derived from the record path rather than rebuilt (#899): the ``@machine``
    strip and the containment check both live in
    :func:`core.session_metadata_path`, so a name that escapes the store raises
    here instead of addressing a spool outside it.
    """
    return core.session_metadata_path(session).parent


def spool_path(session: str) -> Path:
    """Append-only JSONL of messages delivered to *session* via an adapter."""
    return session_state_dir(session) / "inbox-spool.jsonl"


def cursor_path(session: str) -> Path:
    """How far the voice layer has read into the spool."""
    return session_state_dir(session) / "inbox-cursor.json"


def adapter_for(session: str) -> "str | None":
    """The delivery adapter *session* has registered, or None for tmux delivery.

    Returns None for every session that hasn't opted in — which is all of them
    outside this spike.
    """
    try:
        name = core.load_session_metadata(session).get(DELIVERY_KEY)
    except Exception:
        return None
    return name if name in ADAPTERS else None


def deliver(session: str, messages: list) -> "tuple[bool, str]":
    """Hand *messages* to *session*'s adapter. Mirrors ``safe_deliver``'s contract.

    Returns ``(delivered, reason)``. On success the caller unlinks the message
    files exactly as it does after a successful paste, so a message is never
    both spooled and pending.

    Append is all-or-nothing per call: a partial write would leave the caller
    unable to say which messages landed, and the retry would duplicate them.
    """
    adapter = adapter_for(session)
    if adapter != VOICE_ADAPTER:
        return False, "no_adapter"

    path = spool_path(session)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(
            json.dumps({**m.to_dict(), "rendered": m.render()}, ensure_ascii=False) + "\n"
            for m in messages
        )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
            fh.flush()
    except OSError as exc:
        return False, f"spool_write_failed: {exc}"
    return True, "spooled"


def _read_cursor(session: str) -> str:
    """The id of the last message the voice layer acknowledged reading."""
    try:
        with open(cursor_path(session), encoding="utf-8") as fh:
            value = (json.load(fh) or {}).get("last_id")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def _write_cursor(session: str, last_id: str) -> None:
    path = cursor_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"last_id": last_id}, fh)


def _entries(session: str) -> list[dict]:
    """Every message in the spool, in append order. Unparseable lines skipped."""
    path = spool_path(session)
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
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _index_of(entries: list[dict], message_id: str) -> int:
    """Position of *message_id*, or -1 when it isn't in the spool at all.

    -1 also stands for "nothing acked yet", which is what makes the comparison
    in :func:`advance_cursor` work without a special case: an absent cursor and
    a rotated-away cursor are the same thing — everything is unread.
    """
    if not message_id:
        return -1
    for index, entry in enumerate(entries):
        if entry.get("id") == message_id:
            return index
    return -1


def _advance(entries: list[dict], session: str, message_id: str) -> bool:
    target = _index_of(entries, message_id)
    if target < 0:
        # Rotated away (or never ours). Writing it would strand the cursor on an
        # id no read can ever match; sweeping to the tail would lose mail. Refuse
        # and say so — the caller re-reads, which is the cheap failure.
        return False
    if target <= _index_of(entries, _read_cursor(session)):
        return True  # already at or past it: idempotent, and NEVER a rewind
    _write_cursor(session, message_id)
    return True


def advance_cursor(session: str, message_id: "str | None") -> bool:
    """Ack EXACTLY through *message_id* — the fix for the read/ack race (#970).

    ``read_spool(ack=True)`` advances to the spool tail *as it stands at ack
    time*, which is not what the caller read. Every consumer of this spool reads,
    then processes, then acks — the voice notifier deliberately acks only after
    speaking — so mail lands in that window and is cursor-advanced past without
    ever having been read. The cursor was already id-based; this is the missing
    parameter, not a missing mechanism.

    Returns whether the cursor is now at or past *message_id*. False means it is
    not, and there are exactly two ways to get one: an id the spool no longer
    holds, and an empty/None id (a caller asking to ack through nothing). Both
    are returned rather than swallowed — a silently-refused ack is
    indistinguishable from a successful one, and the caller re-announces forever
    with nothing on screen to say why.

    Both halves are priced. Acking too little re-reads a message the owner
    already heard (an annoyance). Acking too much loses one silently — no
    dead-letter, no email, and in a voice channel no screen to notice on. So
    every refusal here fails toward re-reading, and the cursor never moves
    backwards either: a rewind un-reads mail that WAS heard.
    """
    if not message_id:
        return False
    return _advance(_entries(session), session, str(message_id))


def read_spool(
    session: str,
    unread_only: bool = True,
    ack: bool = False,
    ack_through: "str | None" = None,
) -> list[dict]:
    """Read spooled messages, optionally advancing the read cursor.

    ``ack_through=<id>`` acks exactly through that message (see
    :func:`advance_cursor`) and OUTRANKS ``ack``: honouring both would sweep the
    tail, which is the behaviour ``ack_through`` exists to avoid. ``ack=True``
    keeps its old meaning — advance past everything unread — for callers that
    genuinely consume the whole spool in one breath.

    The cursor stores the last-acked message ID, not a line count. A count looks
    simpler and is wrong: rotating or truncating the spool leaves the count
    pointing into a file that no longer has that shape, and the failure is
    silent — new mail reads as already-seen and is never spoken. Message ids are
    unique (``{epoch_ns}-{uuid6}``), so an id that is no longer present means
    the spool was rotated, and the safe answer is "treat everything as unread".
    Re-reading a message is a small annoyance; losing one is the bug.
    """
    entries = _entries(session)
    if not entries:
        return []

    start = 0
    if unread_only:
        found = _index_of(entries, _read_cursor(session))
        if found >= 0:
            start = found + 1

    selected = entries[start:] if unread_only else entries
    if ack_through is not None:
        # NOT `if ack_through:` — an empty string is a caller asking to ack
        # through nothing, which acks nothing. Falling through to the bool path
        # there would sweep the tail from inside the guard against sweeping it.
        # None is the default and means "absent", the closest Python has to the
        # tool layer's presence check.
        if ack_through:
            _advance(entries, session, str(ack_through))
    elif ack:
        _write_cursor(session, str(entries[-1].get("id") or ""))
    return selected


def unread_count(session: str) -> int:
    return len(read_spool(session, unread_only=True, ack=False))
