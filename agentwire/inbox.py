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
import re
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
#
# ``voice`` is the owner speaking to a session through their voice buddy (#985).
# It replaced a ``<voice>`` marker the buddy used to prepend to the message
# BODY: attribution rode inside the text while the slot that actually drives
# behaviour said ``request``. Owner ruling, 2026-08-10:
#
#   * **ACTIVE, not passive.** A voice message is the owner talking; it should
#     drive the session exactly as typing at it would. Making it passive would
#     be a behaviour *reduction* versus the body prefix it replaces, and this
#     slice is a consistency/SSOT change, not a capability cut.
#   * **In ESCALATE_KINDS.** The owner spoke it and walked away. Screenless, a
#     silently dead-lettered voice message is unrecoverable — there is no
#     screen on which to notice the graveyard entry.
#   * **NOT an interrupt.** ``ESCALATE_KINDS`` governs dead-letter escalation,
#     which is a different axis from the interrupt tier. ``escalation`` remains
#     the only kind that pre-empts (see ``_alert_dead_letters``'s promotion and
#     the buddy client's ``isUrgent``).
KINDS = ("note", "done", "request", "escalation", "ingest", "voice")

# Kinds the drain never touches — they route to a subdir and are pull-only.
PASSIVE_KINDS = ("ingest",)

# Broadcast token: deliver to every live agent session except the sender.
BROADCAST_TOKEN = "@all"

# After this many failed/deferred delivery attempts a message is dead-lettered
# rather than retried forever (40 * 60s watchdog tick ≈ 40 min of a session
# being permanently busy/typed-in).
MAX_ATTEMPTS = 40

# A recipient that positively does not exist gets a much shorter window (#694):
# a gone session can't clear its box by itself — it only comes back if someone
# recreates it — so the grace is ~5 watchdog ticks (≈5 min), enough for a
# restart/recreate to land, not the 40-minute busy cap. Counted separately
# (``Message.gone_attempts``) so prior busy penalties don't erode the grace.
GONE_MAX_ATTEMPTS = 5

# Defer reasons that DON'T penalize: the recipient EXISTS but can't take the
# message right now — it is not refusing. Either it's legitimately busy (running
# a long command → unparseable box → "target_busy"; generating with human-queued
# input → the "queued messages" placeholder → "queued_placeholder"; a box holding
# unrecognized-but-static content → "box_static", identical across consecutive
# sweeps ≈ an unknown placeholder, not an actively-typed draft; our own prior
# paste wedged in the box → "stuck_in_box") — or it's usage-limit PARKED
# ("target_parked"), where pasting would corrupt the resume.
#
# Parked is the *most* clearly temporary of these (#872): a park is bounded and
# self-clearing — usage-limit recovery parses the reset time and nudges the
# session afterward — whereas "busy" has no such guarantee. Penalizing it meant a
# park longer than MAX_ATTEMPTS ticks (~40 min, routinely exceeded by a real
# reset window) killed every report-back its workers had filed.
#
# Such messages stay pending indefinitely instead of burning toward dead-letter,
# and deliver once the box frees up or the park clears. The opposite case — a
# recipient that positively does NOT exist — is NOT in this set: "target_gone"
# is penalized on its own fast GONE_MAX_ATTEMPTS cap (#694).
#
# Never dead-lettering also means never triggering the dead-letter owner email,
# so a load-bearing report can now wait hours with nothing announcing it (#879 —
# a gap that #872 widened by admitting the one reason that legitimately lasts
# hours). `agentwire msg inbox` shows a queue on request; `agentwire doctor`
# reports load-bearing messages pending past STALE_PENDING_MS (see
# stale_pending), which is the unprompted surface.
_NO_PENALTY_REASONS = frozenset(
    {"target_busy", "queued_placeholder", "box_static", "stuck_in_box", "target_parked"}
)

# Consecutive sweeps the box must show byte-identical content before the defer
# stops penalizing (see _box_static). Low enough that an unknown placeholder
# costs only a couple of attempts; high enough that a paused human draft eats
# at least a few penalty ticks before being classed as static.
_BOX_STATIC_THRESHOLD = 3

# Load-bearing kinds: a silently-dropped one is a real loss, so on dead-letter it
# is escalated out-of-band (owner email). note is fire-and-forget and ingest
# never auto-delivers, so neither is worth an owner email.
#
# This tuple is the SSOT and every consumer derives from it via load_bearing()
# — see that function for the three hand-written copies #985 deleted.
ESCALATE_KINDS = ("done", "request", "escalation", "voice")

# How long a load-bearing message may sit pending before `doctor` reports it
# (#879). Comfortably longer than any box-state defer — those clear in minutes —
# so the section stays quiet in normal operation and only speaks up for the case
# it exists to catch: a recipient parked or wedged long enough that its workers'
# reports are effectively stranded. Deliberately NOT an owner email: a multi-hour
# park is the EXPECTED shape now, and emailing on it would be the noise that
# gets the whole channel muted (option 3 in #879, declined).
STALE_PENDING_MS = 2 * 60 * 60 * 1000  # 2 hours

_RESERVED_DIRS = {"dead", "sent", ".lock", "ingest"}


def is_passive(kind: str) -> bool:
    """A passive kind is never auto-delivered — it's pull-only (see KINDS)."""
    return kind in PASSIVE_KINDS


def load_bearing(messages: "list[Message]") -> "list[Message]":
    """The subset of *messages* worth surfacing out-of-band (:data:`ESCALATE_KINDS`).

    One implementation, four consumers: the dead-letter owner email, the
    long-pending ``doctor`` section, ``doctor``'s dead-letter section, and the
    ``dead_reports`` badge on ``worktree --list`` / ``--watch``.

    It exists because the last three each carried their own hand-written
    ``("done", "escalation")`` literal, which already disagreed with
    ``ESCALATE_KINDS`` about ``request`` and would have disagreed again about
    ``voice`` (#985) — a dead-lettered voice message emailing the owner on one
    path and vanishing on another. Adding a fifth copy is the failure this
    function exists to prevent; call it instead.
    """
    return [m for m in messages if m.kind in ESCALATE_KINDS]


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
    gone_attempts: int = 0  # ticks the drain observed the target session gone
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
            "gone_attempts": self.gone_attempts,
            "reason": self.reason,
            "dead_ts": self.dead_ts,
            "ref": self.ref,
        }

    def short_id(self) -> str:
        """The 6-char uuid tail of ``id`` (the ``{epoch_ns}-{uuid6}`` suffix)."""
        return self.id.rsplit("-", 1)[-1]

    def render(self) -> str:
        """The one-line message injected on delivery (mirrors [NOTIFY from …]).

        The trailing ``⟨#id6⟩`` token makes every delivered line UNIQUE on the
        recipient's screen (#621). Idempotent-redelivery dedup matches the full
        rendered line on scrollback; without a unique tail a shorter message
        whose text is a prefix of a longer same-sender/kind one (or two
        identical-text report-backs) would substring-collide and be consumed
        without delivery. Landing checks (``message_visible``) key on the full
        whitespace-normalized message (#667), so the tail participates in the
        match rather than weakening it.
        """
        return f"[MSG from {self.sender} · {self.kind}] {self.text}  ⟨#{self.short_id()}⟩"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_ns() -> int:
    # Nanosecond resolution so messages enqueued within the same millisecond
    # still sort by send order (the filename prefix is the ordering key; the
    # uuid suffix is only a uniqueness tiebreaker, never an ordering one).
    return time.time_ns()


def _short_uuid() -> str:
    return uuid.uuid4().hex[:6]


# Session names are agent-controlled (msg_send `to`), so they must never be
# able to path-traverse out of INBOX_ROOT. Worktree session names legitimately
# contain `/` (they nest one directory level per segment — see module
# docstring), so segments are validated individually; `..`, absolute paths,
# and empty names/segments are rejected.
_SESSION_RE = re.compile(r"^[A-Za-z0-9._@-]+(?:/[A-Za-z0-9._@-]+)*$")


def _validate_session(session: str) -> str:
    if not session or not _SESSION_RE.match(session) or ".." in session.split("/"):
        raise ValueError(f"invalid session name: {session!r}")
    return session


def session_dir(session: str) -> Path:
    _validate_session(session)
    path = INBOX_ROOT / session
    # Belt and braces: the regex already forbids traversal, but confine the
    # result to INBOX_ROOT so a validator regression can't escape it.
    if not path.resolve().is_relative_to(INBOX_ROOT.resolve()):
        raise ValueError(f"invalid session name: {session!r}")
    return path


def dead_dir(session: str) -> Path:
    return session_dir(session) / "dead"


def ingest_dir(session: str) -> Path:
    """Where passive (``ingest``) messages live — a reserved subdir the drain
    never walks (it's in ``_RESERVED_DIRS`` and below the top-level glob), so
    these wait silently until pulled."""
    return session_dir(session) / "ingest"


def _box_state_path(session: str) -> Path:
    # No .json suffix so pending_files' *.json glob can never pick it up.
    return session_dir(session) / ".box-state"


def _box_static(session: str, content: str) -> bool:
    """True if this recipient's box has shown identical content ≥ N sweeps.

    Per-recipient last-seen box content persisted next to the inbox. Content
    unchanged across ``_BOX_STATIC_THRESHOLD`` consecutive drain sweeps is not
    an actively-typed human draft — most likely an unrecognized placeholder —
    so the drain defers WITHOUT penalty (like ``target_busy``) instead of
    burning messages toward dead-letter (#669). Never widens delivery: the
    box is still non-empty, so nothing pastes — only the penalty changes.
    """
    path = _box_state_path(session)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        data = {}
    count = int(data.get("count", 0)) + 1 if data.get("content") == content else 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"content": content, "count": count}))
    except OSError:
        pass
    return count >= _BOX_STATIC_THRESHOLD


def _clear_box_state(session: str) -> None:
    try:
        _box_state_path(session).unlink(missing_ok=True)
    except OSError:
        pass


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
            gone_attempts=int(data.get("gone_attempts", 0)),
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


def stale_pending(older_than_ms: int = STALE_PENDING_MS) -> list[tuple[str, Message]]:
    """Load-bearing messages queued longer than *older_than_ms*, oldest first.

    The unprompted surface for the penalty-free defer path (#879). A message
    deferring for a no-penalty reason never dead-letters, so it never triggers
    the dead-letter owner email either — before #872 that was self-limiting
    (every such reason was a short-lived box state), but ``target_parked`` can
    legitimately wait hours. Without this, the only way to notice a `done`
    sitting in a parked parent's queue was to run ``msg inbox`` against that
    exact recipient, already suspecting it.

    Scoped to ESCALATE_KINDS for the same reason the dead-letter email is: a
    lost ``note`` is fire-and-forget and ``ingest`` is pull-only by design, so
    reporting either would be noise that trains people to ignore the section.

    Returns ``(recipient_session, message)`` pairs. Never raises — an
    unreadable inbox yields nothing rather than failing ``doctor``.
    """
    now = _now_ms()
    stale: list[tuple[str, Message]] = []
    try:
        sessions = _iter_pending_sessions()
    except OSError:
        return []
    for session in sessions:
        try:
            messages = list_messages(session)
        except OSError:
            continue
        for msg in load_bearing(messages):
            if msg.ts and now - msg.ts >= older_than_ms:
                stale.append((session, msg))
    stale.sort(key=lambda pair: pair[1].ts)
    return stale


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


def purge_dead(session: "str | None" = None, before_ms: "int | None" = None) -> int:
    """Delete dead-lettered corpses; return the number removed.

    With *session* None, clears every recipient's ``dead/`` dir (the whole
    graveyard); otherwise just that one session's. *before_ms* is an epoch-ms
    cutoff — any corpse that died at-or-after it is kept, so pass ``now - age``
    to clear only stale ones. A corpse with no ``dead_ts`` (pre-schema) counts
    as infinitely old and is always purged when a cutoff is given.

    The dead-letter store holds failed messages a recipient never accepted;
    purging is a human/ops cleanup, never part of the drain.
    """
    if session is not None:
        ddir = dead_dir(session)
        paths = sorted(ddir.glob("*.json")) if ddir.is_dir() else []
    elif INBOX_ROOT.exists():
        paths = sorted(INBOX_ROOT.rglob("dead/*.json"))
    else:
        paths = []

    removed = 0
    for path in paths:
        if before_ms is not None:
            msg = _read_message(path)
            if msg is not None and msg.dead_ts and msg.dead_ts >= before_ms:
                continue  # died at/after the cutoff — keep it
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        _log_event("purged_dead", session=session or "@all", count=removed)
    return removed


def purge_pending(session: str) -> int:
    """Drop a session's *pending* (undelivered) messages; return how many.

    The self-heal escape hatch (#621): when a recipient is wedged into a
    redelivery loop, the only prior recovery was hand-moving JSON files — which
    the recipient's own Bash hook blocks (``rm``). This drops the pending queue
    outright, no empty-box gate, no delivery. Passive (``ingest/``) and dead
    (``dead/``) messages are untouched — this is strictly the active drain queue.
    """
    # Serialize against an in-flight flush_session via the per-session drain lock
    # so we don't yank a file mid-delivery. If a flush holds the lock we still
    # proceed (the operator wants the queue gone) — unlinking under it is benign
    # because flush copies messages into memory first and its own unlink is
    # missing_ok.
    lock = _acquire_lock(session)
    try:
        paths = pending_files(session)
        removed = 0
        for path in paths:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            _log_event("purged_pending", session=session, count=removed)
        return removed
    finally:
        _release_lock(lock)


def gc_sender(sender: str) -> dict:
    """Garbage-collect an exited sender's still-pending outbound (#621).

    Messages live keyed by *recipient*, so when a worktree/session exits nothing
    reaps the report-backs it left undelivered across every inbox — they
    accumulate. This scans all pending queues for that sender and clears them:
    load-bearing kinds (:data:`ESCALATE_KINDS`, which since #985 includes
    ``voice``) are dead-lettered
    (which escalates via the owner-email path so the loss is never silent); the
    rest are dropped. Passive (``ingest``) messages are never auto-delivered, so
    they're left for the recipient to pull. Returns ``{dead, dropped}`` counts.
    """
    dead = dropped = 0
    if not INBOX_ROOT.exists():
        return {"dead": dead, "dropped": dropped}

    # Group this sender's pending files by recipient so each inbox is mutated
    # under its per-session drain lock — serializing against an in-flight
    # flush_session. Without it a kill landing mid-delivery could dead-letter +
    # email "never delivered" for a message that WAS just delivered.
    by_recipient: dict[str, list[Path]] = {}
    for path in INBOX_ROOT.rglob("*.json"):
        parts = path.relative_to(INBOX_ROOT).parts
        if any(p in _RESERVED_DIRS for p in parts[:-1]):
            continue  # skip dead/ sent/ ingest/ .lock/
        msg = _read_message(path)
        if msg is None or msg.sender != sender:
            continue
        recipient = "/".join(parts[:-1])
        if recipient:
            by_recipient.setdefault(recipient, []).append(path)

    for recipient, paths in by_recipient.items():
        lock = _acquire_lock(recipient)
        if lock is None:
            # A flush is draining this inbox right now — its messages are being
            # delivered, not lost. Skip GC for this recipient this round.
            continue
        try:
            newly_dead: list[Message] = []
            for path in paths:
                if not path.exists():
                    continue  # delivered + unlinked just before we locked
                msg = _read_message(path)
                if msg is None or msg.sender != sender:
                    continue
                if msg.kind in ESCALATE_KINDS:
                    msg.dead_ts = _now_ms()
                    msg.reason = "sender_exited"
                    target = dead_dir(msg.to or "unknown") / path.name
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        _write_message(target, msg)
                        path.unlink(missing_ok=True)
                        _log_event(
                            "dead_letter", id=msg.id, to=msg.to, kind=msg.kind,
                            attempts=msg.attempts, reason="sender_exited",
                        )
                        dead += 1
                        newly_dead.append(msg)
                    except OSError:
                        pass
                else:
                    try:
                        path.unlink()
                        dropped += 1
                    except OSError:
                        pass
            # One digest per recipient, not one email per message (#836).
            _escalate_dead_letters(newly_dead, "sender_exited")
        finally:
            _release_lock(lock)

    if dead or dropped:
        _log_event("gc_sender", sender=sender, dead=dead, dropped=dropped)
    return {"dead": dead, "dropped": dropped}


# =============================================================================
# Enqueue + broadcast
# =============================================================================


def live_sessions() -> "set[str] | None":
    """Every live tmux session name, or None when tmux is unreachable.

    ``None`` (no server / no tmux) is deliberately distinct from ``set()``:
    the gone fast-path (#694) and the send-time warning only fire on POSITIVE
    knowledge that tmux is reachable but the target isn't there. With the
    server down everything is equally unreachable — that's an outage, not a
    gone recipient, so callers fall back to the ordinary defer path.
    """
    from .usage_limit import _tmux

    try:
        result = _tmux(["list-sessions", "-F", "#{session_name}"])
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return {s for s in result.stdout.split("\n") if s.strip()}


def _live_agent_sessions() -> list[str]:
    """Every tmux session whose pane 0 runs an agent (Claude/pi)."""
    from . import prompt_router

    sessions = live_sessions()
    if not sessions:
        return []
    return [s for s in sorted(sessions) if prompt_router.is_agent_pane(s, 0)]


def resolve_targets(to: str, sender: "str | None") -> list[str]:
    """Expand a recipient spec into concrete session names.

    ``@all`` fans out to every live agent session except the sender; anything
    else is a single literal session name.
    """
    if to == BROADCAST_TOKEN:
        return [s for s in _live_agent_sessions() if s != sender]
    return [_validate_session(to)]


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


def _fmt_ts(ms: int) -> str:
    if not ms:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


# A single stuck recipient (e.g. a session wrongly parented to a service
# session that can never drain its inbox) can dead-letter dozens of messages
# in one drain pass — cap the per-message detail in the digest so the email
# stays readable instead of dumping an unbounded wall of text.
_ESCALATE_DIGEST_DETAIL_CAP = 10


def _alert_dead_letters(batch: list[Message], reason: str) -> None:
    """Mirror a dead-letter digest to subscribed sessions (#982).

    ``request`` by default: a permanently lost report-back needs somebody to go
    look at ``agentwire msg dead``, but it is not worth cutting across a
    sentence — and the realistic bad case here is a single stuck recipient that
    dead-lettered 147 messages in ~2s (2026-07-19). One alert per BATCH, the
    same coalescing the digest email already does, so that shape buys one
    message rather than 147.

    The one promotion: if what was lost was itself an ``escalation``, the alert
    is an escalation. That is not this module re-deriving urgency — the sender
    already made the judgment, and a *lost interrupt* is precisely the failure
    the tier exists for.

    **Escalatable is not the interrupt tier**, and this is where the two axes
    are easiest to conflate. ``voice`` joined ``ESCALATE_KINDS`` in #985, so a
    lost voice message reaches the owner by email — but the promotion below
    stays keyed on ``escalation`` alone. Widening it to ``voice`` would make
    every routine spoken message an alarm, which is the "retires the tier"
    failure ``fleet_alerts`` exists to avoid.

    Two recursion guards, because an alert is ordinary mail and can dead-letter
    like any other. The recipient guard alone is not enough once more than one
    session subscribes: an alert stranded on the way to subscriber A would
    otherwise be reported to subscriber B, whose own copy is stuck for the same
    reason, once per drain forever. So our own undelivered alerts are dropped
    by SENDER, and every subscriber named as a recipient is excluded.

    ``MACHINE_SENDERS`` rather than ``SENDER`` alone (#1016): lifecycle activity
    is emitted under its own sender and is news by construction. "A 'session
    went idle' notice was lost" is not worth an alert, and it would arrive
    exactly when the fleet is already noisy enough to be stranding mail.
    """
    from . import fleet_alerts

    try:
        mirrored = [m for m in batch if m.sender not in fleet_alerts.MACHINE_SENDERS]
        if not mirrored:
            return
        recipients = sorted({m.to for m in mirrored})
        kind = "escalation" if any(m.kind == "escalation" for m in mirrored) else None
        noun = "message" if len(mirrored) == 1 else "messages"
        kinds = ", ".join(sorted({m.kind for m in mirrored}))
        fleet_alerts.emit_for(
            "dead_letter",
            f"{len(mirrored)} load-bearing {noun} ({kinds}) to "
            f"{', '.join(recipients)} were never delivered and have been "
            f"dead-lettered (last defer reason: {reason}). They are recoverable "
            f"with `agentwire msg dead`, but nobody has seen them.",
            kind=kind,
            exclude=recipients,
        )
    except Exception as exc:  # best-effort; never break the drain
        _log_event("dead_letter_alert_failed", count=len(batch), error=str(exc))


def _escalate_dead_letters(messages: list[Message], reason: str) -> None:
    """Email the owner once per batch when load-bearing report-backs dead-letter.

    ``done`` / ``request`` / ``escalation`` / ``voice`` are load-bearing — a
    silently-dropped one is a real loss, so we surface it out-of-band via the
    shared Resend wiring
    (the same owner-escalation channel usage-limit parking uses). ``note`` and
    ``ingest`` are not escalated.

    *messages* is everything dead-lettered by one caller's batch (one drain
    pass for one recipient, or one sender's GC sweep) — a single digest email
    covers the whole batch instead of one email per message, so a recipient
    that's been permanently undeliverable for a while (e.g. parented to a
    service session that never drains) can't spam the owner's inbox one email
    per stuck message (147 individual emails in ~2s, 2026-07-19). Best-effort:
    a missing key or send failure must never break the drain — each corpse
    already sits in ``dead/`` for ``agentwire msg dead``.
    """
    batch = load_bearing(messages)
    if not batch:
        return
    _alert_dead_letters(batch, reason)
    try:
        import socket

        from .channels.email import send_email

        host = socket.gethostname()
        if len(batch) == 1:
            msg = batch[0]
            subject = (
                f"[agentwire] undelivered {msg.kind}: {msg.sender} → {msg.to} (dead-lettered)"
            )
        else:
            to = batch[0].to
            subject = (
                f"[agentwire] {len(batch)} undelivered messages → {to} (dead-lettered)"
            )
        noun = "message" if len(batch) == 1 else "messages"
        verb = "was" if len(batch) == 1 else "were"
        lines = [
            f"{len(batch)} load-bearing {noun} on `{host}` {verb} never delivered "
            f"and {'has' if len(batch) == 1 else 'have'} been dead-lettered "
            f"(last defer reason: {reason}).",
            "",
        ]
        for msg in batch[:_ESCALATE_DIGEST_DETAIL_CAP]:
            lines += [
                f"- **{msg.kind}** {msg.sender} → {msg.to}, sent {_fmt_ts(msg.ts)}, "
                f"dead-lettered {_fmt_ts(msg.dead_ts)}, {msg.attempts} attempts:",
                "  ```",
                f"  {msg.text}",
                "  ```",
            ]
        remaining = len(batch) - _ESCALATE_DIGEST_DETAIL_CAP
        if remaining > 0:
            lines.append(f"- ...and {remaining} more.")
        lines += [
            "",
            f"Saved in the dead-letter store — review with "
            f"`agentwire msg dead -s {batch[0].to}`.",
        ]
        result = send_email(subject=subject, body="\n".join(lines))
        _log_event(
            "dead_letter_escalated", to=batch[0].to, count=len(batch), reason=reason,
            ok=bool(getattr(result, "success", False)),
        )
    except Exception as exc:  # escalation is best-effort; never break the drain
        _log_event("dead_letter_escalate_failed", to=batch[0].to, count=len(batch), error=str(exc))


def _bump_attempts(messages: list[Message], reason: str = "") -> int:
    """Increment attempts on each pending message; dead-letter over the cap.

    ``reason`` is the defer reason that caused this pass; it's stamped onto the
    message so a dead-lettered one carries *why* it never got delivered.
    Returns the number dead-lettered this pass.

    ``target_gone`` additionally bumps the per-message ``gone_attempts``
    counter and dead-letters at the fast ``GONE_MAX_ATTEMPTS`` cap (#694) — a
    gone session can't un-go by itself, so it gets minutes of grace (enough
    for a recreate to land), not the 40-minute busy window. The counter is
    separate from ``attempts`` so busy penalties accrued while the target
    lived don't erode the grace, and cumulative across gone observations —
    a target that flaps in and out without ever accepting delivery still
    burns out on schedule.
    """
    dead = 0
    newly_dead: list[Message] = []
    for msg in messages:
        if msg.path is None:
            continue
        if reason in _NO_PENALTY_REASONS:
            # The recipient exists but can't take it right now — busy (long
            # command / human-queued input / wedged paste) or usage-limit parked
            # — not refusing. Never penalize; delivers once the prompt frees up
            # or the park clears. Surfaced by `agentwire msg inbox` on request,
            # and by `agentwire doctor` once it's been waiting past
            # STALE_PENDING_MS (#879).
            msg.reason = reason
            try:
                _write_message(msg.path, msg)
            except OSError:
                pass
            continue

        msg.attempts += 1
        msg.reason = reason
        if reason == "target_gone":
            msg.gone_attempts += 1
        if msg.attempts >= MAX_ATTEMPTS or msg.gone_attempts >= GONE_MAX_ATTEMPTS:
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
                newly_dead.append(msg)
            except OSError:
                pass
        else:
            try:
                _write_message(msg.path, msg)
            except OSError:
                pass
    # One digest email for the whole batch (all share the same recipient —
    # `messages` is always one session's inbox), not one per message (#836).
    _escalate_dead_letters(newly_dead, reason)
    return dead


def _dedup_landed(session: str, messages: list[Message]) -> list[Message]:
    """Consume (unlink) every message whose render() is already on scrollback.

    The load-bearing #621 fix. ``safe_deliver`` confirms submission by polling
    the input box back to empty; under host load that confirm false-negatives
    even though the paste *landed* and the recipient saw it. Retaining a landed
    message re-injects it on every idle tick — forever. So before (and after) a
    paste we check the recipient's scrollback per-message: any message whose own
    first-line fragment is visible has demonstrably landed → unlink it. Only
    truly-absent messages stay pending.

    Per-message keying (not the coalesced blob) so a partial landing consumes
    exactly the visible subset. A strict fragment check (never the generic
    ``"[Pasted text"`` placeholder fallback) so a stray placeholder can't mark
    every message delivered. A message that scrolled past the 200-line window
    reads as not-visible → kept (safe direction: retry, never silent-drop).
    """
    from .session_ready import message_on_scrollback, scrollback

    capture = scrollback(session, 0)
    consumed: list[Message] = []
    for msg in messages:
        if msg.path is None:
            continue
        if message_on_scrollback(capture, msg.render()):
            msg.path.unlink(missing_ok=True)
            consumed.append(msg)
    if consumed:
        _log_event(
            "delivered_dedup", to=session, count=len(consumed),
            kinds=[m.kind for m in consumed],
        )
    return consumed


def _cohort_held(session: str, messages: list[Message]) -> list[Message]:
    """Messages from *session*'s still-pending fan-out children (#852).

    Their owner is the parent's ``wait --children`` join, not the drain. Keyed
    on the cohort being ACTIVE (pending children, deadline not passed), so an
    expired ledger releases its messages immediately instead of waiting for the
    sweeper to clean it up. Fails open (returns nothing held) on any error, so
    a broken cohort ledger can never withhold a message.
    """
    try:
        from . import cohort

        if not cohort.blocking(session):
            return []
        pending = set(cohort.pending(session))
    except Exception:
        return []
    if not pending:
        return []
    return [m for m in messages if m.sender in pending]


def flush_session(session: str, force: bool = False) -> dict:
    """Attempt to drain one session's inbox now.

    Delivers oldest-first, coalescing all queued messages into a single paste
    (one submit) when the box is empty. On any refusal the messages stay put,
    their ``attempts`` bump, and over the cap they dead-letter. Never raises.

    *force* (the ``msg flush --force`` escape hatch) bypasses the empty-box /
    busy gate and pastes regardless — for an operator un-wedging a stuck queue,
    accepting that it may land mid-draft. The ``safe_deliver`` safety guards
    (gone / parked / non-agent / live-dialog) are never bypassed.
    """
    from . import prompt_router

    lock = _acquire_lock(session)
    if lock is None:
        return {"session": session, "delivered": 0, "deferred": True, "reason": "locked"}
    try:
        messages = list_messages(session)
        if not messages:
            return {"session": session, "delivered": 0, "deferred": False, "reason": "empty"}

        # Cohort hold (#852): a report from a child this session is still
        # waiting on belongs to `agentwire wait --children`, which reads it
        # straight off disk and consumes it before tearing the child down.
        # Pasting it into the parent's box instead would (a) race that
        # collection, leaving the child unresolved until its deadline, and
        # (b) push a long report through the one delivery path #851 shows is
        # fragile. Deferred WITHOUT penalty and bounded by the cohort's own
        # deadline: once the ledger resolves or the sweeper drops it, these
        # deliver normally.
        held = _cohort_held(session, messages)
        if held:
            messages = [m for m in messages if m not in held]
            _log_event("deferred", to=session, count=len(held), reason="cohort_held")
            if not messages:
                return {"session": session, "delivered": 0, "deferred": True,
                        "reason": "cohort_held"}

        # Non-tmux delivery (EXPERIMENTAL, voice-layer spike). A recipient that
        # registered a delivery adapter has no pane to paste into, so every gate
        # below it is inapplicable — and the gone gate would actively kill its
        # mail, since tmux is reachable and the recipient legitimately isn't in
        # the session list. Sits AFTER the cohort hold on purpose: a held report
        # belongs to `wait --children`, which reads it off disk, and spooling it
        # here would consume it out from under that collection. Inert for every
        # session without a `delivery` key in metadata.json — i.e. all of them.
        from .voice_layer import delivery as _delivery

        if _delivery.adapter_for(session) is not None:
            ok, reason = _delivery.deliver(session, messages)
            if ok:
                for msg in messages:
                    if msg.path is not None:
                        msg.path.unlink(missing_ok=True)
                _log_event(
                    "delivered", to=session, count=len(messages),
                    kinds=[m.kind for m in messages], adapter=reason,
                )
                return {
                    "session": session, "delivered": len(messages),
                    "deferred": False, "reason": "delivered",
                }
            dead = _bump_attempts(messages, reason)
            _log_event("deferred", to=session, count=len(messages), reason=reason)
            return {
                "session": session, "delivered": 0, "deferred": True,
                "reason": reason, "dead": dead,
            }

        # Gone gate FIRST (#694): a recipient that positively doesn't exist can
        # never clear a box, and the ordinary gates misread it — capturing a
        # gone session parses as "no box" → target_busy, a NO-penalty defer, so
        # a done to a stale parent once sat queued ~24h instead of burning out.
        # Gone is its own penalized reason with the fast GONE_MAX_ATTEMPTS cap.
        # Only positive knowledge counts: with tmux unreachable live_sessions()
        # is None and the ordinary defer path applies.
        live = live_sessions()
        if live is not None and session not in live:
            dead = _bump_attempts(messages, "target_gone")
            _log_event("deferred", to=session, count=len(messages), reason="target_gone")
            return {
                "session": session, "delivered": 0, "deferred": True,
                "reason": "target_gone", "dead": dead,
            }

        pre_consumed = 0
        if not force:
            # Collision guard FIRST (cheap, and refuses dialogs/busy too via None).
            # But first: a prior tick may have LANDED these and false-negatived the
            # confirm (#621). If they're already on scrollback, consume them now
            # instead of waiting for the box to free up to re-paste a duplicate.
            consumed = _dedup_landed(session, messages)
            if consumed:
                pre_consumed = len(consumed)
                messages = [m for m in messages if m.path is not None and m.path.exists()]
                if not messages:
                    return {
                        "session": session, "delivered": pre_consumed,
                        "deferred": False, "reason": "delivered",
                    }

            # SGR-preserving capture so dim ghost/autosuggest text inside the
            # box reads as empty instead of starving delivery (#669).
            visible = prompt_router.capture(session, 0, escapes=True)
            box_content = prompt_router.input_box_content_sgr(visible)

            if box_content is None:
                # Target is busy (input box not located). Defer.
                dead = _bump_attempts(messages, "target_busy")
                _log_event("deferred", to=session, count=len(messages), reason="target_busy")
                return {
                    "session": session, "delivered": pre_consumed, "deferred": True,
                    "reason": "target_busy", "dead": dead,
                }

            if box_content != "":
                # Our own message sitting in the box = a prior paste whose Enter
                # was swallowed (#689). Heal it: Enter-only finish_submit — NEVER
                # a re-paste, so the #621 idempotency dedup keeps holding. Unlink
                # only once submission is confirmed; otherwise stay pending
                # without penalty (the target isn't refusing, our own delivery
                # is wedged).
                stuck = [
                    m for m in messages
                    if "".join(m.render().split()) in "".join(box_content.split())
                ]
                if stuck:
                    from .session_ready import finish_submit

                    if finish_submit(session, stuck[0].render()):
                        for m in stuck:
                            if m.path is not None:
                                m.path.unlink(missing_ok=True)
                        _log_event(
                            "stuck_submitted", to=session, count=len(stuck),
                            kinds=[m.kind for m in stuck],
                        )
                        _clear_box_state(session)
                        remaining = [m for m in messages if m not in stuck]
                        if remaining:
                            _bump_attempts(remaining, "target_busy")
                        return {
                            "session": session,
                            "delivered": pre_consumed + len(stuck),
                            "deferred": bool(remaining),
                            "reason": "delivered" if not remaining else "target_busy",
                        }
                    dead = _bump_attempts(messages, "stuck_in_box")
                    _log_event(
                        "deferred", to=session, count=len(messages),
                        reason="stuck_in_box",
                    )
                    return {
                        "session": session, "delivered": pre_consumed,
                        "deferred": True, "reason": "stuck_in_box", "dead": dead,
                    }

                # Box is not empty. We never bypass this to protect human drafts. But
                # the "queued messages" placeholder is a BUSY signal, not a draft:
                # defer WITHOUT penalty (like target_busy) so a generating-with-queued
                # session doesn't burn report-backs toward dead-letter. Either way we
                # never paste — only the penalty decision differs.
                if prompt_router.is_queued_placeholder(box_content):
                    reason = "queued_placeholder"
                elif _box_static(session, box_content):
                    # Same unrecognized content for N straight sweeps — an
                    # unknown placeholder, not an active draft. Still deferred
                    # (never pasted), but no longer burning toward dead-letter.
                    reason = "box_static"
                else:
                    reason = "box_not_empty"
                dead = _bump_attempts(messages, reason)
                _log_event("deferred", to=session, count=len(messages), reason=reason)
                return {
                    "session": session, "delivered": pre_consumed, "deferred": True,
                    "reason": reason, "dead": dead,
                }

            _clear_box_state(session)  # box is empty — reset the static counter

        rendered = "\n".join(m.render() for m in messages)
        delivered, reason = prompt_router.safe_deliver(session, 0, rendered)
        if not delivered:
            # delivery_unverified means the box-cleared confirm failed — but the
            # paste may have LANDED. Consume any message now visible on scrollback
            # (idempotent delivery) so a false-negative can't cause re-injection.
            # Other reasons (gone/parked/non-agent/dialog) never pasted, so there's
            # nothing to dedup.
            consumed = (
                _dedup_landed(session, messages)
                if reason == "delivery_unverified"
                else []
            )
            consumed_ids = {m.id for m in consumed}
            remaining = [m for m in messages if m.id not in consumed_ids]
            if not remaining:
                return {
                    "session": session, "delivered": pre_consumed + len(consumed),
                    "deferred": False, "reason": "delivered",
                }
            dead = _bump_attempts(remaining, reason)
            _log_event("deferred", to=session, count=len(remaining), reason=reason)
            return {
                "session": session, "delivered": pre_consumed + len(consumed),
                "deferred": True, "reason": reason, "dead": dead,
            }

        for msg in messages:
            if msg.path is not None:
                msg.path.unlink(missing_ok=True)
        _log_event(
            "delivered", to=session, count=len(messages),
            kinds=[m.kind for m in messages],
        )
        return {
            "session": session, "delivered": pre_consumed + len(messages),
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
