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
KINDS = ("note", "done", "request", "escalation", "ingest")

# Kinds the drain never touches — they route to a subdir and are pull-only.
PASSIVE_KINDS = ("ingest",)

# Broadcast token: deliver to every live agent session except the sender.
BROADCAST_TOKEN = "@all"

# After this many failed/deferred delivery attempts a message is dead-lettered
# rather than retried forever (40 * 60s watchdog tick ≈ 40 min of a session
# being permanently busy/typed-in).
MAX_ATTEMPTS = 40

# Defer reasons that DON'T penalize: the target is legitimately busy — running a
# long command (unparseable box → "target_busy") or generating with human-queued
# input (the "queued messages" placeholder → "queued_placeholder"), or the box
# holds unrecognized-but-static content ("box_static": identical across
# consecutive sweeps ≈ an unknown placeholder, not an actively-typed draft).
# Such messages stay pending forever instead of burning toward dead-letter;
# doctor / worktree --watch surface them, and they deliver once the box frees up.
_NO_PENALTY_REASONS = frozenset({"target_busy", "queued_placeholder", "box_static"})

# Consecutive sweeps the box must show byte-identical content before the defer
# stops penalizing (see _box_static). Low enough that an unknown placeholder
# costs only a couple of attempts; high enough that a paused human draft eats
# at least a few penalty ticks before being classed as static.
_BOX_STATIC_THRESHOLD = 3

# Load-bearing kinds: a silently-dropped one is a real loss, so on dead-letter it
# is escalated out-of-band (owner email). note is fire-and-forget and ingest
# never auto-delivers, so neither is worth an owner email.
ESCALATE_KINDS = ("done", "request", "escalation")

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
    load-bearing kinds (``done``/``request``/``escalation``) are dead-lettered
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
                        _escalate_dead_letter(msg, "sender_exited")
                    except OSError:
                        pass
                else:
                    try:
                        path.unlink()
                        dropped += 1
                    except OSError:
                        pass
        finally:
            _release_lock(lock)

    if dead or dropped:
        _log_event("gc_sender", sender=sender, dead=dead, dropped=dropped)
    return {"dead": dead, "dropped": dropped}


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


def _escalate_dead_letter(msg: Message, reason: str) -> None:
    """Email the owner when a load-bearing report-back dead-letters.

    ``done`` / ``request`` / ``escalation`` are load-bearing — a silently-dropped
    one is a real loss, so we surface it out-of-band via the shared Resend wiring
    (the same owner-escalation channel usage-limit parking uses). ``note`` and
    ``ingest`` are not escalated. Best-effort: a missing key or send failure must
    never break the drain — the corpse already sits in ``dead/`` for
    ``agentwire msg dead``.
    """
    if msg.kind not in ESCALATE_KINDS:
        return
    try:
        import socket

        from .channels.email import send_email

        host = socket.gethostname()
        subject = (
            f"[agentwire] undelivered {msg.kind}: {msg.sender} → {msg.to} (dead-lettered)"
        )
        body = "\n".join([
            f"A **{msg.kind}** message from **{msg.sender}** to **{msg.to}** on "
            f"`{host}` was never delivered after {msg.attempts} attempts and has "
            f"been dead-lettered.",
            "",
            f"- **Kind:** {msg.kind}",
            f"- **From:** {msg.sender}",
            f"- **To:** {msg.to}",
            f"- **Last defer reason:** {reason}",
            f"- **Sent:** {_fmt_ts(msg.ts)}",
            f"- **Dead-lettered:** {_fmt_ts(msg.dead_ts)}",
            "",
            "Message text:",
            "",
            "```",
            msg.text,
            "```",
            "",
            f"Saved in the dead-letter store — review with `agentwire msg dead -s {msg.to}`.",
        ])
        result = send_email(subject=subject, body=body)
        _log_event(
            "dead_letter_escalated", id=msg.id, to=msg.to, kind=msg.kind,
            ok=bool(getattr(result, "success", False)),
        )
    except Exception as exc:  # escalation is best-effort; never break the drain
        _log_event("dead_letter_escalate_failed", id=msg.id, to=msg.to, error=str(exc))


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
        if reason in _NO_PENALTY_REASONS:
            # Target is busy (long command, or generating with human-queued input),
            # not refusing — never penalize. Surfaced via `doctor` / `worktree
            # --watch`; delivers once the prompt is empty/idle.
            msg.reason = reason
            try:
                _write_message(msg.path, msg)
            except OSError:
                pass
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
                _escalate_dead_letter(msg, reason)
            except OSError:
                pass
        else:
            try:
                _write_message(msg.path, msg)
            except OSError:
                pass
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
