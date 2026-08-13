"""Owner notification for unattended damage-control blocks — spooled, throttled,
digested (#925).

Every ``ask``-tier command an unattended (scheduler) session hits is blocked
fail-closed, and until now every one of those blocks sent its own email the
instant it fired. Measured over 14 days that was 96 emails and accelerating —
28 in one day — of which 52 were the SAME rule (``core.ambiguous-command``) and
most were the same rule in the same session, looping. An owner who deletes 28
identical emails a day is an owner who stops reading the 29th, which is the one
that matters. Unthrottled notification is not "loud", it is off.

The shape here is not invented: it is the one this repo already uses twice for
out-of-band owner escalation, and deliberately reuses rather than adding a
third dialect.

* :mod:`agentwire.auth_expired` — a persisted ``escalated_at`` stamp and an
  ``ESCALATE_TTL`` of one hour, so a persistent condition emails on first
  sighting and then at most hourly.
* :func:`agentwire.inbox._escalate_dead_letters` — ONE digest email per batch
  rather than one per item, after a recipient that had been undeliverable for a
  while produced 147 individual emails in ~2 seconds (2026-07-19).

This module is both at once, because a block has neither shape on its own: it
is a stream of discrete events (like dead letters) that describes a persistent
condition (like an outage). So events are **spooled**, aggregated by
``(rule_id, session)``, and released as a digest at most once per
:data:`THROTTLE`.

Three properties are load-bearing:

**The audit log is untouched.** The hook calls ``log_blocked`` BEFORE it calls
this notifier, on a separate path, and nothing here can suppress it. Throttling
the email while also throttling the record would trade spam for blindness —
``agentwire safety logs`` stays complete whatever this module decides, and the
digest points at it.

**Repeats do not re-notify, but they are never lost.** A task looping on one
blocked command is one *fact*, not forty, so a ``(rule_id, session)`` pair
already reported within :data:`DEDUP_TTL` does not by itself trigger another
digest. Its count keeps accumulating in the spool and rides along on the next
digest that does fire, and :data:`DEDUP_TTL` expiry means a condition that
never resolves still re-reports daily rather than going permanently quiet.

**Nothing here may raise.** The caller is a fire-and-forget subprocess spawned
off a security hook that has ALREADY blocked the command. An exception in the
notifier cannot un-block anything; it can only turn a clean block into a
confusing one. Every public entry point returns rather than propagates.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

# How often the owner may hear about unattended blocks, at most. Matches
# ``auth_expired.ESCALATE_TTL`` and ``prompt_router.NO_PARENT_ESCALATE_TTL``
# deliberately — this is the same channel and the same owner, and three
# different windows would just make the inbox's cadence unpredictable.
THROTTLE = timedelta(hours=1)

# How long a ``(rule_id, session)`` pair stays "already reported". Within this
# window its repeats ride along in a digest but never trigger one; past it the
# pair is news again, so a condition nobody fixed re-surfaces daily instead of
# being silently swallowed forever.
DEDUP_TTL = timedelta(hours=24)

# Distinct ``(rule_id, session)`` pairs held in the spool. Far above any real
# burst (the 14-day record has 96 blocks across a handful of pairs); it exists
# so a pathological fleet cannot grow the state file without bound. Blocks
# arriving past the cap are counted in ``overflow`` and named in the digest —
# dropped silently, they would make the digest's total a lie.
PAIR_CAP = 200

# Example commands kept per pair. Enough to recognise the shape ("it's the
# for-loop over memory stores again"), few enough that a digest stays readable.
SAMPLES_PER_PAIR = 3

# Pairs rendered in full in one digest, ordered by count. The rest are summed
# into a trailing line rather than dumped.
DIGEST_PAIR_CAP = 12


def _config_dir() -> Path:
    """Read through the MODULE, not a from-import (#902).

    ``from .core import CONFIG_DIR`` binds at import time, so a test that
    patches ``core.CONFIG_DIR`` is silently ignored and this writes to the real
    ``~/.agentwire``. Same reason :func:`agentwire.auth_expired._config_dir`
    and :func:`agentwire.core.role_prompts_dir` are functions.
    """
    from . import core

    return Path(core.CONFIG_DIR)


def state_path() -> Path:
    """The ONE spool. Machine-wide — the owner has one inbox, not one per session."""
    return _config_dir() / "safety" / "unattended-blocks.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


@contextlib.contextmanager
def _locked():
    """Serialize read-modify-write across processes via an flock sidecar.

    Not optional. The notifier is spawned per block by a hook that fires in
    every session at once, so two blocks landing in the same second are the
    normal case, not the race-condition edge case — unlocked, one would clobber
    the other's spool entry and the digest would undercount.
    """
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_state() -> dict:
    try:
        data = json.loads(state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict) -> None:
    """Atomic — a torn write must not leave an unparseable spool behind."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def pair_key(rule_id: str, session: str) -> str:
    return f"{rule_id or 'unknown'}\x1f{session or 'unknown'}"


def _unpair(key: str) -> tuple[str, str]:
    rule, _, session = key.partition("\x1f")
    return rule, session


# ---------------------------------------------------------------------------
# Spooling
# ---------------------------------------------------------------------------


def _spool(state: dict, rule_id: str, session: str, reason: str, command: str,
           now: datetime) -> dict:
    """Fold one block into the pending aggregate, keyed by ``(rule_id, session)``."""
    pending = state.setdefault("pending", {})
    key = pair_key(rule_id, session)
    entry = pending.get(key)
    if entry is None:
        if len(pending) >= PAIR_CAP:
            state["overflow"] = int(state.get("overflow", 0)) + 1
            return state
        entry = {"count": 0, "first_ts": now.isoformat(), "reason": reason,
                 "samples": []}
        pending[key] = entry
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last_ts"] = now.isoformat()
    entry["reason"] = reason or entry.get("reason", "")
    samples = entry.setdefault("samples", [])
    if command and command not in samples and len(samples) < SAMPLES_PER_PAIR:
        samples.append(command)
    return state


def _due(state: dict, now: datetime) -> bool:
    """Should a digest go out right now?

    Two independent gates, and both must open:

    1. **Rate** — :data:`THROTTLE` has elapsed since the last email. This is
       the absolute bound on how often the owner is interrupted.
    2. **News** — at least one pending pair has not been reported within
       :data:`DEDUP_TTL`. This is what turns "a task looping on one command"
       into one email rather than one per hour forever.

    A spool that is due on rate but not on news stays pending: its counts keep
    accumulating and are reported by the next digest that IS news, or by this
    one once ``DEDUP_TTL`` makes the pair news again.
    """
    pending = state.get("pending") or {}
    if not pending:
        return False
    last = _parse(state.get("last_email_at"))
    if last is not None and now - last < THROTTLE:
        return False
    reported = state.get("reported") or {}
    for key in pending:
        seen = _parse(reported.get(key))
        if seen is None or now - seen >= DEDUP_TTL:
            return True
    return False


def _gc_reported(reported: dict, now: datetime) -> dict:
    """Forget pairs long past :data:`DEDUP_TTL` so the state file stays bounded."""
    keep = {}
    for key, ts in reported.items():
        seen = _parse(ts)
        if seen is not None and now - seen < DEDUP_TTL * 2:
            keep[key] = ts
    return keep


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def _fmt_window(state: dict, now: datetime) -> str:
    first = None
    for entry in (state.get("pending") or {}).values():
        ts = _parse(entry.get("first_ts"))
        if ts is not None and (first is None or ts < first):
            first = ts
    if first is None:
        return "just now"
    minutes = int((now - first).total_seconds() // 60)
    if minutes < 1:
        return "in the last minute"
    if minutes < 120:
        return f"in the last {minutes} minutes"
    return f"in the last {minutes // 60} hours"


def render_digest(state: dict, now: datetime | None = None) -> tuple[str, str]:
    """``(subject, body)`` for the pending spool. Pure — no I/O, so it is testable."""
    now = now or _now()
    pending = state.get("pending") or {}
    pairs = sorted(pending.items(), key=lambda kv: -int(kv[1].get("count", 0)))
    total = sum(int(e.get("count", 0)) for _, e in pairs)
    overflow = int(state.get("overflow", 0))
    host = socket.gethostname()

    if len(pairs) == 1:
        rule, session = _unpair(pairs[0][0])
        subject = (f"[agentwire] {total} unattended block"
                   f"{'' if total == 1 else 's'}: {rule} in {session}")
    else:
        subject = (f"[agentwire] {total + overflow} unattended blocks across "
                   f"{len(pairs)} rule/session pairs on {host}")

    lines = [
        f"{total + overflow} unattended (scheduled) action"
        f"{'' if total + overflow == 1 else 's'} on `{host}` "
        f"{_fmt_window(state, now)} required human confirmation, so "
        f"{'it was' if total + overflow == 1 else 'they were'} **blocked** "
        f"(fail-closed). Nothing was executed.",
        "",
    ]
    for key, entry in pairs[:DIGEST_PAIR_CAP]:
        rule, session = _unpair(key)
        count = int(entry.get("count", 0))
        lines.append(f"- **{count} ×** `{rule}` in session **{session}**")
        if entry.get("reason"):
            lines.append(f"  - {entry['reason']}")
        for sample in entry.get("samples") or []:
            lines.append(f"  - `{sample}`")
    hidden = pairs[DIGEST_PAIR_CAP:]
    if hidden:
        lines.append(f"- ...and {sum(int(e.get('count', 0)) for _, e in hidden)} "
                     f"more across {len(hidden)} further pairs.")
    if overflow:
        lines.append(f"- ...plus {overflow} block(s) on pairs beyond the "
                     f"{PAIR_CAP}-pair spool cap (counted, not detailed).")

    rules = sorted({_unpair(k)[0] for k, _ in pairs})
    lines += [
        "",
        "Full record (never throttled — this digest is, the audit log is not):",
        "```",
        "agentwire safety logs --today",
        "```",
        "",
        "To permit a specific action for an unattended task in future, add its "
        "rule id to that task's `unattended_allow` in `.agentwire.tasks.yml` "
        "(or to `unattended_allow` in `~/.agentwire/damagecontrol.yml` "
        f"globally). Ids seen here: {', '.join(f'`{r}`' for r in rules)}.",
        "",
        f"Further blocks are digested; the next email is at most one per "
        f"{int(THROTTLE.total_seconds() // 3600)}h.",
    ]
    return subject, "\n".join(lines)


def _send(state: dict, now: datetime) -> dict:
    """Send the digest and fold the result back into *state*.

    Only a SUCCESSFUL send clears the spool and stamps ``last_email_at`` —
    following ``auth_expired._escalate``'s explicit trade. A failed send that
    counted as delivered would lose the report entirely, which is strictly
    worse than retrying it on the next block.
    """
    subject, body = render_digest(state, now)
    try:
        from .channels.email import send_email

        result = send_email(subject=subject, body=body)
        ok = bool(getattr(result, "success", False))
    except Exception:  # email not configured, no network, … — never propagate
        return state
    if not ok:
        return state

    reported = _gc_reported(dict(state.get("reported") or {}), now)
    for key in (state.get("pending") or {}):
        reported[key] = now.isoformat()
    state["reported"] = reported
    state["pending"] = {}
    state["overflow"] = 0
    state["last_email_at"] = now.isoformat()
    return state


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def record_block(rule_id: str, session: str, reason: str, command: str,
                 now: datetime | None = None) -> dict:
    """Spool one unattended block; send a digest if one is due.

    Returns ``{"spooled": bool, "emailed": bool}`` for callers that want to say
    what happened. Never raises: the command is already blocked and the audit
    line is already written, so the worst this may do is stay quiet.
    """
    now = now or _now()
    try:
        with _locked():
            state = read_state()
            _spool(state, rule_id, session, reason, command, now)
            emailed = False
            if _due(state, now):
                before = state.get("last_email_at")
                state = _send(state, now)
                emailed = state.get("last_email_at") != before
            write_state(state)
        return {"spooled": True, "emailed": emailed}
    except Exception:
        return {"spooled": False, "emailed": False}


def tick(now: datetime | None = None) -> dict:
    """Flush a due spool from the watchdog (``agentwire limits tick``, 60s).

    Without this, a burst's tail waits for the *next* block to be delivered —
    so a task that is blocked ten times and then gives up would report the
    first block and silently sit on the other nine. Pure housekeeping: it never
    spools anything, only releases what a due spool already holds.
    """
    now = now or _now()
    try:
        with _locked():
            state = read_state()
            if not _due(state, now):
                return {"emailed": False, "pending": len(state.get("pending") or {})}
            before = state.get("last_email_at")
            state = _send(state, now)
            emailed = state.get("last_email_at") != before
            write_state(state)
            return {"emailed": emailed, "pending": len(state.get("pending") or {})}
    except Exception:
        return {"emailed": False, "pending": 0}
