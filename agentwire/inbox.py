"""Polite agent-to-agent messaging — the ``agentwire msg`` inbox.

A durable, non-interrupting channel for sessions to talk amongst themselves.
Unlike ``agentwire send`` / ``session_send`` (which paste into the prompt and
press Enter *immediately* — forceful control, and the right tool when you
need it), ``msg`` drops a typed message into a per-recipient file inbox and
only injects it when the recipient's Claude Code input box is empty and the
pane is a safe delivery target. A worker reporting back can no longer clobber
a half-typed human draft.

Layout under ``~/.agentwire/inbox/``::

    <session>/                      # one dir per recipient session
      1718323456789-a1b2c3.json     # <epoch_ms>-<short_uuid>.json (sort = order)
      .lock/                        # mkdir-based drain lock
      dead/                         # messages dropped after MAX_ATTEMPTS
    .tick.lock                      # global flock guarding tick()

"ls is the protocol" — same pattern as Council's ``council/inbox.py``.
Sorting by filename = delivery order. Worktree session names contain ``/`` and
nest a directory level (mirrors ``usage_limit.state_path``); the tick walks
the tree and reconstructs the name from the path.

Delivery = ``safe_deliver`` guards (parked / non-agent / live-dialog refusals
+ verified paste) **plus** the new ``prompt_is_empty`` collision guard.
"""

from __future__ import annotations

import errno
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from agentwire.utils.event_log import append_event

INBOX_ROOT = Path.home() / ".agentwire" / "inbox"
EVENTS_FILE = Path.home() / ".agentwire" / "inbox-events.jsonl"

# Typed message enum, Overstory-inspired — kept deliberately small; this is a
# mailbox, not a workflow engine.
#
# ``ingest`` is the PASSIVE kind: it is never auto-delivered (the watchdog skips
# it), so it never drives the recipient into a turn. It lands silently in an
# ``ingest/`` subdir and waits there until the recipient *voluntarily* pulls it
# (``msg pull``). This is the "awareness without being driven" primitive —
# correspondents drop a passive pointer; the anchor pulls on the human's cue.
KINDS = ("note", "done", "request", "escalation", "ingest")

# Kinds the drain never touches — they route to a subdir and are pull-only.
PASSIVE_KINDS = ("ingest",)

# Broadcast token: deliver to every live agent session except the sender.
BROADCAST_TOKEN = "@all"

# After this many failed/deferred delivery attempts a message is dead-lettered
# rather than retried forever (40 * 60s watchdog tick ≈ 40 min of a session
# being permanently busy/typed-in).
MAX_ATTEMPTS = 40

_RESERVED_DIRS = {"dead", "sent", ".lock", "ingest"}


def is_passive(kind: str) -> bool:
    """A passive kind is never auto-delivered — it's pull-only (see KINDS)."""
    return kind in PASSIVE_KINDS


# =============================================================================
# Message model + paths
# =============================================================================


@dataclass
class Message:
    id: str
    sender: str  # serialized as "from"
    to: str
    kind: str
    text: str
    ts: int  # epoch ms
    attempts: int = 0
    reason: str = ""  # last defer reason (why delivery kept failing)
    dead_ts: int = 0  # epoch ms when dead-lettered (0 = still live)
    ref: str = ""  # optional machine-readable pointer (e.g. a report path) — for ingest
    path: Path | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from": self.sender,
            "to": self.to,
            "kind": self.kind,
            "text": self.text,
            "ts": self.ts,
            "attempts": self.attempts,
            "reason": self.reason,
            "dead_ts": self.dead_ts,
            "ref": self.ref,
        }

    def render(self) -> str:
        """The one-line prefix injected on delivery (mirrors [NOTIFY from …])."""
        return f"[MSG from {self.sender} · {self.kind}] {self.text}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_ns() -> int:
    # Nanosecond resolution so messages enqueued within the same millisecond
    # still sort by send order (the filename prefix is the ordering key; the
    # uuid suffix is only a uniqueness tiebreaker, never an ordering one).
    return time.time_ns()


def _short_uuid() -> str:
    return uuid.uuid4().hex[:6]


def session_dir(session: str) -> Path:
    return INBOX_ROOT / session


def dead_dir(session: str) -> Path:
    return session_dir(session) / "dead"


def ingest_dir(session: str) -> Path:
    """Where passive (``ingest``) messages live — a reserved subdir the drain
    never walks (it's in ``_RESERVED_DIRS`` and below the top-level glob), so
    these wait silently until pulled."""
    return session_dir(session) / "ingest"


def _log_event(event: str, **fields) -> None:
    record = {"ts": _now_ms(), "event": event, **fields}
    append_event(EVENTS_FILE, record)


def _read_message(path: Path) -> "Message | None":
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Message(
            id=str(data["id"]),
            sender=str(data.get("from", "unknown")),
            to=str(data.get("to", "")),
            kind=str(data.get("kind", "note")),
            text=str(data.get("text", "")),
            ts=int(data.get("ts", 0)),
            attempts=int(data.get("attempts", 0)),
            reason=str(data.get("reason", "")),
            dead_ts=int(data.get("dead_ts", 0)),
            ref=str(data.get("ref", "")),
            path=path,
        )
    except (KeyError, ValueError, TypeError):
        return None


def _write_message(path: Path, msg: Message) -> None:
    """Atomic write: *.tmp then rename (same dir = atomic on rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(msg.to_dict(), indent=2))
    os.replace(tmp, path)


def pending_files(session: str) -> list[Path]:
    """A session's queued message files, oldest first (excludes dead/sent)."""
    sdir = session_dir(session)
    if not sdir.is_dir():
        return []
    return sorted(sdir.glob("*.json"))


def list_messages(session: str) -> list[Message]:
    return [m for m in (_read_message(f) for f in pending_files(session)) if m]


def ingest_files(session: str) -> list[Path]:
    """A session's queued passive (ingest) message files, oldest first."""
    idir = ingest_dir(session)
    if not idir.is_dir():
        return []
    return sorted(idir.glob("*.json"))


def list_ingest(session: str) -> list[Message]:
    """Peek passive (ingest) messages without consuming them."""
    return [m for m in (_read_message(f) for f in ingest_files(session)) if m]


def pull_ingest(session: str) -> list[Message]:
    """Read AND remove all passive (ingest) messages — the voluntary pull.

    The inverse of being pushed: the recipient calls this on its own cadence
    (e.g. the anchor when the human says "what's ready?"). Returns oldest-first.
    The watchdog never delivers or dead-letters these, so pulling is the only
    way they leave the inbox — the durable content lives in the files they
    point at, not in the message itself.
    """
    msgs = list_ingest(session)
    for m in msgs:
        if m.path is not None:
            m.path.unlink(missing_ok=True)
    if msgs:
        _log_event("pulled", to=session, count=len(msgs))
    return msgs


def list_dead(session: str) -> list[Message]:
    """A session's dead-lettered messages, oldest-died first."""
    ddir = dead_dir(session)
    if not ddir.is_dir():
        return []
    return [m for m in (_read_message(f) for f in sorted(ddir.glob("*.json"))) if m]


def dead_sessions() -> list[str]:
    """Recipient session names that have any dead-lettered messages.

    Walks the tree so worktree session names (which contain ``/`` and nest a
    directory level) are reconstructed from the path. The ``dead`` component is
    always the parent of the message file, so the session is everything before
    it.
    """
    if not INBOX_ROOT.exists():
        return []
    found: set[str] = set()
    for path in INBOX_ROOT.rglob("dead/*.json"):
        parts = path.relative_to(INBOX_ROOT).parts
        session = "/".join(parts[:-2])  # drop "<...>/dead/<file>.json"
        if session:
            found.add(session)
    return sorted(found)


# =============================================================================
# Enqueue + broadcast
# =============================================================================


def _live_agent_sessions() -> list[str]:
    """Every tmux session whose pane 0 runs an agent (Claude/pi)."""
    from . import prompt_router
    from .usage_limit import _tmux

    try:
        result = _tmux(["list-sessions", "-F", "#{session_name}"])
    except Exception:
        return []
    if result.returncode != 0:
        return []
    sessions = [s for s in result.stdout.split("\n") if s.strip()]
    return [s for s in sessions if prompt_router.is_agent_pane(s, 0)]


def resolve_targets(to: str, sender: "str | None") -> list[str]:
    """Expand a recipient spec into concrete session names.

    ``@all`` fans out to every live agent session except the sender; anything
    else is a single literal session name.
    """
    if to == BROADCAST_TOKEN:
        return [s for s in _live_agent_sessions() if s != sender]
    return [to]


def enqueue(
    to: str, text: str, kind: str = "note", sender: "str | None" = None, ref: str = ""
) -> list[Message]:
    """Drop a message into one or more recipient inboxes. Returns what was written."""
    if kind not in KINDS:
        raise ValueError(f"invalid kind: {kind!r} (expected one of {KINDS})")
    if not text.strip():
        raise ValueError("message text is empty")

    sender = sender or "unknown"
    targets = resolve_targets(to, sender)
    written: list[Message] = []
    for target in targets:
        ns = _now_ns()
        msg = Message(
            id=f"{ns}-{_short_uuid()}",
            sender=sender,
            to=target,
            kind=kind,
            text=text,
            ts=ns // 1_000_000,  # epoch ms (schema), derived from the same clock
            attempts=0,
            ref=ref,
        )
        # Passive kinds land in the ingest/ subdir, which the drain never walks
        # — so they wait silently until the recipient pulls them.
        base = ingest_dir(target) if is_passive(kind) else session_dir(target)
        path = base / f"{msg.id}.json"
        msg.path = path
        _write_message(path, msg)
        _log_event(
            "enqueued", id=msg.id, **{"from": sender}, to=target, kind=kind,
            passive=is_passive(kind), broadcast=(to == BROADCAST_TOKEN),
        )
        written.append(msg)
    return written


# =============================================================================
# Drain (flush)
# =============================================================================


def _acquire_lock(session: str) -> "Path | None":
    """mkdir-based per-session drain lock (mirrors queue-processor.sh)."""
    lock = session_dir(session) / ".lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.mkdir()
        return lock
    except FileExistsError:
        return None
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return None
        return None


def _release_lock(lock: "Path | None") -> None:
    if lock is None:
        return
    try:
        lock.rmdir()
    except OSError:
        pass


def _bump_attempts(messages: list[Message], reason: str = "") -> int:
    """Increment attempts on each pending message; dead-letter over the cap.

    ``reason`` is the defer reason that caused this pass; it's stamped onto the
    message so a dead-lettered one carries *why* it never got delivered.
    Returns the number dead-lettered this pass.
    """
    dead = 0
    for msg in messages:
        if msg.path is None:
            continue
        msg.attempts += 1
        msg.reason = reason
        if msg.attempts >= MAX_ATTEMPTS:
            msg.dead_ts = _now_ms()
            target = dead_dir(msg.to or "unknown") / msg.path.name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_message(target, msg)
                msg.path.unlink(missing_ok=True)
                _log_event(
                    "dead_letter", id=msg.id, to=msg.to, kind=msg.kind,
                    attempts=msg.attempts, reason=reason,
                )
                dead += 1
            except OSError:
                pass
        else:
            try:
                _write_message(msg.path, msg)
            except OSError:
                pass
    return dead


def flush_session(session: str) -> dict:
    """Attempt to drain one session's inbox now.

    Delivers oldest-first, coalescing all queued messages into a single paste
    (one submit) when the box is empty. On any refusal the messages stay put,
    their ``attempts`` bump, and over the cap they dead-letter. Never raises.
    """
    from . import prompt_router

    lock = _acquire_lock(session)
    if lock is None:
        return {"session": session, "delivered": 0, "deferred": True, "reason": "locked"}
    try:
        messages = list_messages(session)
        if not messages:
            return {"session": session, "delivered": 0, "deferred": False, "reason": "empty"}

        # Collision guard FIRST (cheap, and refuses dialogs/busy too via None).
        if not prompt_router.prompt_is_empty(session, 0):
            dead = _bump_attempts(messages, "box_not_empty")
            _log_event("deferred", to=session, count=len(messages), reason="box_not_empty")
            return {
                "session": session, "delivered": 0, "deferred": True,
                "reason": "box_not_empty", "dead": dead,
            }

        rendered = "\n".join(m.render() for m in messages)
        delivered, reason = prompt_router.safe_deliver(session, 0, rendered)
        if not delivered:
            dead = _bump_attempts(messages, reason)
            _log_event("deferred", to=session, count=len(messages), reason=reason)
            return {
                "session": session, "delivered": 0, "deferred": True,
                "reason": reason, "dead": dead,
            }

        for msg in messages:
            if msg.path is not None:
                msg.path.unlink(missing_ok=True)
        _log_event(
            "delivered", to=session, count=len(messages),
            kinds=[m.kind for m in messages],
        )
        return {
            "session": session, "delivered": len(messages),
            "deferred": False, "reason": "delivered",
        }
    except Exception as exc:  # draining must never break the watchdog
        _log_event("flush_failed", to=session, error=str(exc))
        return {"session": session, "delivered": 0, "deferred": True, "reason": "error"}
    finally:
        _release_lock(lock)


def _iter_pending_sessions() -> list[str]:
    """Recipient session names that currently have queued messages.

    Walks the tree so worktree session names (which contain ``/`` and nest a
    directory level) are reconstructed from the path; skips dead/sent/lock.
    """
    if not INBOX_ROOT.exists():
        return []
    found: set[str] = set()
    for path in INBOX_ROOT.rglob("*.json"):
        parts = path.relative_to(INBOX_ROOT).parts
        if any(p in _RESERVED_DIRS for p in parts[:-1]):
            continue
        session = "/".join(parts[:-1])
        if session:
            found.add(session)
    return sorted(found)


def tick() -> dict:
    """One drain pass over every inbox with queued messages.

    Rides ``agentwire limits tick`` (after the usage-limit + prompt-router
    sweeps). Globally locked so a manual ``msg flush`` can't race the
    watchdog. Never raises.
    """
    import fcntl

    INBOX_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = INBOX_ROOT / ".tick.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"skipped": "tick already running"}

        flushed, deferred = [], []
        for session in _iter_pending_sessions():
            result = flush_session(session)
            if result.get("delivered"):
                flushed.append(result)
            elif result.get("deferred"):
                deferred.append(result)
        return {"flushed": flushed, "deferred": deferred}
