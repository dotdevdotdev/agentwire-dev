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

Delivery here means *handed to the buddy's spool*, an append-only JSONL file
the voice layer reads when the owner asks. It is deliberately a PULL, not a
push: this slice never interrupts. See ``docs/wiki/voice-layer.md``.

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


def read_spool(session: str, unread_only: bool = True, ack: bool = False) -> list[dict]:
    """Read spooled messages. ``ack=True`` advances the read cursor past them.

    The cursor stores the last-acked message ID, not a line count. A count looks
    simpler and is wrong: rotating or truncating the spool leaves the count
    pointing into a file that no longer has that shape, and the failure is
    silent — new mail reads as already-seen and is never spoken. Message ids are
    unique (``{epoch_ns}-{uuid6}``), so an id that is no longer present means
    the spool was rotated, and the safe answer is "treat everything as unread".
    Re-reading a message is a small annoyance; losing one is the bug.
    """
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

    start = 0
    if unread_only:
        last_id = _read_cursor(session)
        if last_id:
            for index, entry in enumerate(entries):
                if entry.get("id") == last_id:
                    start = index + 1
                    break

    selected = entries[start:] if unread_only else entries
    if ack and entries:
        _write_cursor(session, str(entries[-1].get("id") or ""))
    return selected


def unread_count(session: str) -> int:
    return len(read_spool(session, unread_only=True, ack=False))
