"""Fleet detectors, addressed as typed mail — the producer side of the kind axis (#982).

Every detector in this repo already knows how to tell the OWNER something: the
shared Resend wiring, best-effort, throttled by whatever state the detector
already keeps. What none of them could do is tell a *session*. This module is
that half, and it is deliberately the same shape as the email one: one call,
never raises, and inert when nobody is listening.

**The kind IS the policy.** ``inbox`` already types mail — ``note`` / ``done`` /
``request`` / ``escalation`` — and consumers key on the type rather than
re-deriving urgency for themselves. So the only real decision this module makes
is which kind each detector gets, and that decision lives in one place as data:
:data:`DETECTOR_KINDS`. The rulings, and what each one costs, are in
``docs/wiki/sessions/messaging.md``.

**Why the ruling is the hard part.** ``escalation`` is the one kind a consumer
may act on out of turn. A producer that over-fires does not add noise, it
retires the tier — a recipient that learns escalations are usually ignorable
will ignore the one that wasn't. So the bar is not "is this true?" (all five
candidate detectors are true when they fire) but "can this clear without a
human, and is something burning while it waits?" Two detectors pass. One is
demoted to ``note`` because it self-heals, one has a floor of ``request`` with
an inherit rule, and one is not wired at all. That is the whole design.

**Subscription is a LEASE, not a flag.** A subscriber records
:data:`SUBSCRIBE_KEY` in its ``metadata.json`` (the #871 SSOT store, so there
is no second registry to drift) carrying an expiry it must renew. The reason is
the dormancy failure: the inbox's own liveness gates are about *pasting into a
pane*, so a recipient that reads its mail some other way never reads as gone
the way a dead tmux session does. A permanent flag would keep producing into a
queue nobody is draining, and hand the whole backlog over at once whenever that
recipient next came up — every message arriving with the priority it was sent
with, long after any of it was actionable. An expired lease fails QUIET, which
is the correct direction for a producer whose expensive failure is
over-production.

Nothing here knows what a subscriber is. Anything with a session record and an
inbox can lease one, and no detector below gains a dependency on any of them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

#: Session-record key holding the subscription lease.
SUBSCRIBE_KEY = "fleet_alerts"

#: Sender stamped on every alert. Load-bearing, not cosmetic: it is how the
#: dead-letter detector recognizes its own undelivered mail and declines to
#: alert about the alert (see ``inbox._escalate_dead_letters``).
SENDER = "fleet-alerts"

#: How long a lease is good for without renewal. Long enough that a working
#: session renewed at startup keeps hearing about the fleet for a full day of
#: use; short enough that a subscriber which ran once last week is not still
#: accumulating interrupts. A long-running subscriber past this simply goes
#: quiet until it renews — see the module docstring on failing quiet.
DEFAULT_LEASE = timedelta(hours=12)

#: THE RULING. Which detector's event earns which kind — pinned as data so that
#: changing what may interrupt is an edit somebody has to justify, not a literal
#: buried at a call site. Rationale per entry:
#:
#: ``auth_expired`` → **escalation**. Machine-wide: every subsequent turn on the
#:   host is refused, and nothing clears it but a human running ``/login``.
#:   Bounded to once per ``auth_expired.ESCALATE_TTL`` per outage, machine-wide,
#:   by the outage record itself.
#:
#: ``blocked_pane_no_parent`` → **escalation**. A ROOT session blocked on an
#:   interactive prompt has, by design, nobody to route to; it is stalled until
#:   a human answers, and some of those prompts have deadlines. Bounded to once
#:   per ``prompt_router.NO_PARENT_ESCALATE_TTL`` per distinct prompt.
#:
#: ``usage_limit_park`` → **note**. Demoted deliberately. The park is
#:   self-healing — the reset time is parsed, the resume nudge is armed, and the
#:   owner's own email says "no action needed". Worth hearing at a gap; it
#:   cannot earn an interrupt when there is nothing to interrupt anyone FOR.
#:
#: ``dead_letter`` → **request**, with one inherit rule: if what was lost was
#:   itself an ``escalation``, the alert is an escalation, because the fleet
#:   already made that judgment and losing it is the failure. The floor is
#:   ``request`` because the realistic bad case is a permanently-stuck
#:   recipient — 147 dead letters in ~2s, once — and that shape must not be
#:   able to buy 147 interrupts. Bounded to one alert per dead-letter BATCH,
#:   the same coalescing the digest email already does.
#:
#: Not here, and on purpose: ``worktree --dangling``. It has no autonomous
#: trigger (only ``doctor`` and the explicit flag, both run by a human already
#: looking at the output) and no per-finding throttle state to reuse, so a
#: producer would re-announce the same durable, passive condition on every
#: invocation. A dangling PR is not burning anything while it waits.
DETECTOR_KINDS: dict[str, str] = {
    "auth_expired": "escalation",
    "blocked_pane_no_parent": "escalation",
    "usage_limit_park": "note",
    "dead_letter": "request",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _config_dir() -> Path:
    """Read through the MODULE, never a from-import (#902).

    ``from .core import CONFIG_DIR`` freezes the value at import time, so a test
    patching ``core.CONFIG_DIR`` is silently ignored and this writes into the
    operator's real store.
    """
    from . import core

    return Path(core.CONFIG_DIR)


def events_path() -> Path:
    return _config_dir() / "fleet-alerts-events.jsonl"


def log_event(event: str, **fields) -> None:
    """Append telemetry. Best-effort — never break a detector."""
    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": _now().isoformat(), "event": event, **fields}) + "\n")
    except (OSError, TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


def subscribe(session: str, lease: timedelta = DEFAULT_LEASE) -> dict:
    """Lease fleet alerts for *session*, or renew an existing lease.

    Writes into the session's existing record rather than replacing it — the
    store holds conversation identity (#871), and clobbering that to register
    for mail would be a data-destruction bug wearing a feature's clothes.

    **Requires a record to already exist**, and that is about the store rather
    than about typos. ``load_session_metadata`` returns ``{}`` for a name it has
    never seen, so writing the lease back created a record for a session that
    does not exist — a ``{}`` entry that ``core.recorded_sessions()`` counts and
    nothing distinguishes from a real one. A verb that can mint junk into the
    SSOT for conversation identity is the wrong shape however it is reached; a
    subscription is a property OF a session, so a session it can invent is not
    a subscription at all.
    """
    from . import core

    if not core.session_metadata_path(session).exists():
        raise ValueError(
            f"no session record for {session!r} — subscribe an existing session "
            f"(see `agentwire list`), or register it first"
        )
    meta = core.load_session_metadata(session)
    now = _now()
    meta[SUBSCRIBE_KEY] = {
        "since": now.isoformat(),
        "expires_at": (now + lease).isoformat(),
    }
    core.store_session_metadata(session, meta)
    _write_index([*_read_index(), session])
    log_event("subscribed", session=session, expires_at=meta[SUBSCRIBE_KEY]["expires_at"])
    return meta[SUBSCRIBE_KEY]


def unsubscribe(session: str) -> bool:
    """Drop *session*'s lease. True iff there was one."""
    from . import core

    meta = core.load_session_metadata(session)
    if SUBSCRIBE_KEY not in meta:
        return False
    meta.pop(SUBSCRIBE_KEY)
    core.store_session_metadata(session, meta)
    _write_index([n for n in _read_index() if n != session])
    log_event("unsubscribed", session=session)
    return True


def subscription(session: str, now: "datetime | None" = None) -> "dict | None":
    """*session*'s live lease, or None (absent, malformed, or expired).

    A malformed value reads as "not subscribed" rather than "subscribed
    forever": a typo must not become a permanent interrupt licence.
    """
    from . import core

    try:
        record = core.load_session_metadata(session).get(SUBSCRIBE_KEY)
    except Exception:
        return None
    if not isinstance(record, dict):
        return None
    try:
        expires = datetime.fromisoformat(str(record.get("expires_at")))
    except (TypeError, ValueError):
        return None
    return record if expires > (now or _now()) else None


def subscribers_index_path() -> Path:
    """Candidate list of subscribers — who to ASK, never who IS subscribed."""
    return _config_dir() / "fleet-alerts" / "subscribers.json"


def _read_index() -> list[str]:
    try:
        names = json.loads(subscribers_index_path().read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _write_index(names: list[str]) -> None:
    path = subscribers_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(set(names)), indent=2))
    os.replace(tmp, path)


def subscribers(now: "datetime | None" = None) -> list[str]:
    """Every session holding a live lease, sorted. ``[]`` on any failure.

    **Two-level, and the levels are not equal.** The index names CANDIDATES;
    each candidate's own record decides. That ordering is what keeps a cache
    from becoming a second source of truth — a stale index entry (unregistered,
    killed, expired) is verified away on read, so the index can only ever cause
    us to ask a question, never to answer one.

    The reason it exists is cost, measured rather than assumed: walking the
    record store took ~326ms against 1155 records on this machine, and one
    caller of this function sits on the SYNCHRONOUS permission-hook path. A
    feature that taxes the product's hot path to discover nobody is listening
    is not "inert". With no index file the answer is one failed ``stat``.

    The failure direction is the same one the lease chose: a lost or truncated
    index means fewer alerts, never more, and ``agentwire alerts reindex``
    rebuilds it from the records that are authoritative anyway.
    """
    at = now or _now()
    return sorted(name for name in _read_index() if subscription(name, at) is not None)


def reindex() -> list[str]:
    """Rebuild the index by walking the record store. Returns live subscribers.

    The expensive path, on purpose and on demand only: this is what
    ``agentwire alerts reindex`` runs after a lost index, not something an
    alert path ever reaches.
    """
    from . import core

    try:
        names = core.recorded_sessions()
    except Exception:
        return []
    live = sorted(name for name in names if subscription(name) is not None)
    try:
        _write_index(live)
    except OSError as exc:
        log_event("reindex_write_failed", error=str(exc))
    return live


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def emit(
    text: str,
    *,
    kind: str,
    ref: str = "",
    exclude: Iterable[str] = (),
    detector: str = "",
) -> list[str]:
    """Enqueue one typed alert per live subscriber. Returns who was reached.

    Never raises on anything environmental: a detector's job is to detect, and
    a full disk or an unwritable inbox must not turn "we caught the outage" into
    an exception thrown from the catch. One failing target does not abandon the
    others, and a failure is logged rather than swallowed — a producer that
    cannot be distinguished from a quiet fleet is the #885 shape again.

    An unknown *kind* DOES raise: it can only be a coding bug at a call site,
    and silently dropping it would leave a detector that looks wired and is not.
    """
    from . import inbox

    if kind not in inbox.KINDS:
        raise ValueError(f"invalid alert kind: {kind!r} (expected one of {inbox.KINDS})")

    skip = set(exclude)
    targets = [name for name in subscribers() if name not in skip]
    reached: list[str] = []
    for target in targets:
        try:
            inbox.enqueue(target, text, kind=kind, sender=SENDER, ref=ref)
        except Exception as exc:
            log_event("emit_failed", to=target, kind=kind, detector=detector, error=str(exc))
            continue
        reached.append(target)
    if reached:
        log_event("emitted", to=reached, kind=kind, detector=detector)
    return reached


def emit_for(detector: str, text: str, **kwargs) -> list[str]:
    """:func:`emit` with the kind taken from the ruling in :data:`DETECTOR_KINDS`.

    The call sites use this so that "what may interrupt" is answered in one
    place. ``kind=`` may still be passed explicitly for the one detector with an
    inherit rule (dead letters), which is why that override exists at all.
    """
    kind = kwargs.pop("kind", None) or DETECTOR_KINDS[detector]
    return emit(text, kind=kind, detector=detector, **kwargs)
