"""Detect an expired Claude login and stop dispatching into it (#906).

The failure this exists for, measured on 2026-08-04 (#867): a scheduled
dispatch did everything right — session created, agent launched, a 20,433-byte
prompt submitted at ``08:00:20.785Z`` — and the turn was rejected 15 ms later:

.. code-block:: json

    {"model": "<synthetic>",
     "content": [{"type": "text", "text": "Login expired · Please run /login"}],
     "usage": {"input_tokens": 0, "output_tokens": 0},
     "error": "authentication_failed", "isApiErrorMessage": true}

Zero tokens, no model call, ``turn_duration: 16ms``, transcript ends. Nothing
noticed. ``memory-manager`` then sat until its ceiling and reported
``incomplete — Timeout waiting for task completion``, which describes the
symptom and actively misleads about the cause; ``ai-morning-briefing`` hit the
same outage four hours later and burned the scheduler's full 14400s. Six hours
of dispatch time, and three investigation passes chasing a guardrail, a
dispatcher, a timeout and a paste race.

**The transcript is the detector, and the pane deliberately is not.** Claude
Code renders the phrase inline as an ordinary assistant message, so it also
appears in any pane that merely *quotes* or reviews the incident — the pane
investigating #867 had "Login expired · Please run /login" on screen all day.
A pane-text rule would buy that false-positive class, and a false positive
here **halts scheduling**, which is strictly worse than the hang it replaces.
The transcript's ``error`` field is a structured fact about a turn that
actually happened, so that is what is keyed on. This is the opposite trade
from :mod:`usage_limit`, and for a concrete reason: the usage-limit signal is
a *live select-menu* that can be proven live (nothing renders after it), and
this one cannot.

Recovery is a property of the same signal, not a separate mechanism: only the
**last** assistant turn in a transcript decides, so a session that auth-failed
and then took a real turn reads as healthy with nothing to reset.

The machine-wide part matters as much as the detection. An expired login is
not per-task — every subsequent dispatch hits it — so one detection records a
single outage state, emails the owner ONCE (throttled, following the
dead-letter and #905 no-parent escalation precedents rather than inventing a
third channel), and later dispatches fail fast instead of each burning its own
timeout. The state carries :data:`OUTAGE_TTL` so it cannot wedge the scheduler
indefinitely: after it, one dispatch is let through as a probe, which now
fails in seconds rather than hours.

On a pre-flight credential check (#906 item 3): there is no cheap, reliable
local signal to gate on. Claude Code keeps credentials in the macOS Keychain,
where reading them needs ``security find-generic-password -w`` — an
interactive authorization prompt, which unattended is a worse hang than the
bug. Where a plaintext ``~/.claude/.credentials.json`` does exist, its
``expiresAt`` is the *access* token's, which lapses routinely and is refreshed
silently; gating dispatch on it would false-alarm constantly. The outage state
below IS the cheap pre-flight — a single local file read, no network — it just
costs one detection to arm.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The transcript's structured error value for an expired/refused login. Keyed
# on exactly this — never widened to "any api error", because a rate-limit or
# an overloaded upstream is transient and retryable, while this one provably
# cannot succeed until a human runs `/login`.
AUTH_ERROR = "authentication_failed"

# What Claude Code renders for it. Used only to quote the operator a
# recognizable line in the email/summary — never as a detection signal (see
# the module docstring).
RENDERED = "Login expired · Please run /login"

# How far back into a transcript to read. A hung run's file is a few KB; a
# long interactive one is megabytes, and only its tail can be the last turn.
TAIL_BYTES = 256 * 1024

# How long a recorded outage keeps gating dispatch before one probe is allowed
# through. Bounded on purpose: a stale flag that never cleared would halt every
# scheduled task on the machine, which is a worse failure than the hang. Each
# fresh detection refreshes it, so a real outage keeps gating; a resolved one
# costs at most one fast failure to notice.
OUTAGE_TTL = timedelta(minutes=30)

# Owner-escalation throttle. Follows prompt_router's no-parent escalation
# (#905) and the dead-letter digest: an out-of-band email, sent on the first
# sighting and then at most once an hour while the outage persists. Unthrottled
# this would be one email per dispatch per outage.
ESCALATE_TTL = timedelta(hours=1)


def _config_dir() -> Path:
    """Read through the MODULE, not a from-import (#902).

    ``from .core import CONFIG_DIR`` binds the value at import time, so a test
    (or anything else) that patches ``core.CONFIG_DIR`` is silently ignored and
    the code writes to the real ``~/.agentwire``. That is the same trap
    ``core.role_prompts_dir()`` exists to avoid, and here it would mean a test
    suite scribbling a real outage gate onto the operator's machine.
    """
    from . import core

    return Path(core.CONFIG_DIR)


def state_path() -> Path:
    """The ONE outage record. Machine-wide, not per-session or per-task."""
    return _config_dir() / "auth-expired" / "state.json"


def events_path() -> Path:
    return _config_dir() / "auth-expired-events.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log_event(event: str, **fields) -> None:
    """Append an event. Best-effort — telemetry must never break a dispatch."""
    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({"ts": _now().isoformat(), "event": event, **fields}) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Detection — the transcript
# ---------------------------------------------------------------------------


def _tail_lines(path: Path, limit: int = TAIL_BYTES) -> list[str]:
    """The last complete lines of *path*, bounded by *limit* bytes.

    The first line of a mid-file read is almost always a fragment, so it is
    dropped rather than fed to ``json.loads`` — a partial row must read as
    "nothing to see", never as a parse error that aborts the scan.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
                chunk = f.read()
                chunk = chunk.split(b"\n", 1)[1] if b"\n" in chunk else b""
            else:
                chunk = f.read()
    except OSError:
        return []
    return [ln for ln in chunk.decode("utf-8", "replace").split("\n") if ln.strip()]


def last_assistant_turn(path: Path) -> dict | None:
    """The last ``assistant`` row in *path*, or None.

    "Last" is the whole point. A transcript that auth-failed at 08:00 and took
    a real turn at 09:00 has recovered, and reporting it as expired would gate
    the scheduler on a resolved outage. Reading only the final assistant row
    makes recovery fall out of detection instead of needing a reset.
    """
    found = None
    for line in _tail_lines(path):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(row, dict) and row.get("type") == "assistant":
            found = row
    return found


def row_is_auth_failure(row: dict | None) -> bool:
    """Does *row* carry the expired-login rejection?

    Keyed on the structured ``error`` field, not on the rendered text, so a
    reworded message keeps working and an assistant turn that merely *says*
    the phrase (an agent reporting on this very incident) never matches.

    Rewording is proven, not assumed: the two real outages on disk render
    DIFFERENTLY — "Login expired · Please run /login" (2026-08-04, Claude Code
    2.1.221) and "Not logged in · Please run /login" (2026-07-07, 2.1.201) —
    and share this one field. Both are fixtured.

    The residual risk is RESTRUCTURING, not rewording: if a future version
    nests the error (say under ``message.error`` or an ``error.type``), this
    returns False and the detector goes quiet rather than loud. Nothing here
    can catch that on its own — the check to run when a Claude Code upgrade
    lands is that ``error`` is still a top-level string on an api-error row.
    """
    if not isinstance(row, dict):
        return False
    return row.get("error") == AUTH_ERROR


def transcript_auth_failure(path: Path) -> bool:
    """True iff *path*'s most recent assistant turn was refused for auth."""
    return row_is_auth_failure(last_assistant_turn(path))


# ---------------------------------------------------------------------------
# Detection — locating the transcripts to read
# ---------------------------------------------------------------------------


def recorded_transcripts(session: str) -> list[Path]:
    """Transcripts named by the session's own launch record (#871).

    The strong path: agentwire MINTS the conversation id and records it with
    the launch cwd, so this addresses the exact file rather than guessing.
    Returns [] when the record predates #871 (or the session was never
    recorded) — the caller falls back to :func:`touched_transcripts`, which is
    still evidence rather than a guess.
    """
    from .core import load_session_metadata
    from .history import PROJECTS_DIR, encode_project_path

    try:
        meta = load_session_metadata(session)
    except Exception:
        return []
    cwd = meta.get("cwd_at_launch")
    ids = meta.get("conversation_ids") or []
    if not cwd or not ids:
        return []
    base = Path(PROJECTS_DIR) / encode_project_path(str(cwd))
    return [p for p in (base / f"{cid}.jsonl" for cid in ids) if p.exists()]


def touched_transcripts(project_path, since: float) -> list[Path]:
    """Transcripts in *project_path*'s history dir written since *since*.

    The fallback for a session whose launch record predates #871 — which is
    exactly ``memory-manager``'s shape on 2026-08-04, so a detector that only
    handled the recorded path could not have seen the incident it was written
    for.

    Deliberately NOT "the newest ``.jsonl`` in the directory", the guess
    CLAUDE.md warns against: this is scoped to one project dir AND to files
    written during the window the caller is asking about, so a hit is a
    transcript this run actually produced.
    """
    from .history import PROJECTS_DIR, encode_project_path

    base = Path(PROJECTS_DIR) / encode_project_path(str(project_path))
    try:
        entries = list(base.glob("*.jsonl"))
    except OSError:
        return []
    out = []
    for p in entries:
        try:
            if p.stat().st_mtime >= since:
                out.append(p)
        except OSError:
            continue
    return sorted(out)


def detect(session: str, project_path=None, since: float | None = None) -> dict | None:
    """Is *session*'s last turn an expired-login rejection?

    Returns a detail dict (never a bare bool — the caller has to be able to
    say WHICH transcript proved it) or None. Recorded transcripts first; the
    touched-since fallback only when the record can't name one.
    """
    for path in recorded_transcripts(session):
        if transcript_auth_failure(path):
            return {"session": session, "transcript": str(path), "source": "recorded"}
    if project_path is not None and since is not None:
        for path in touched_transcripts(project_path, since):
            if transcript_auth_failure(path):
                return {"session": session, "transcript": str(path), "source": "touched"}
    return None


# ---------------------------------------------------------------------------
# Machine-wide outage state
# ---------------------------------------------------------------------------


def read_state() -> dict | None:
    try:
        return json.loads(state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_state(state: dict) -> None:
    """Atomic — a torn write must not leave an unparseable gate behind."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def clear_state() -> bool:
    """Drop the outage record. True iff one was there.

    Called from ``completion.wait_for_completion_signal``'s success path: a
    written task summary is proof a turn ran, which is proof the login works.
    That is what makes the operator-facing "reopens on the first successful
    turn" a fact rather than a description of behavior nothing implements —
    the mismatch #906 itself is about, at a smaller scale. ``OUTAGE_TTL``
    remains the backstop for a fleet that isn't completing anything.
    """
    try:
        state_path().unlink()
        log_event("outage_cleared")
        return True
    except OSError:
        return False


def outage_active(now: datetime | None = None) -> dict | None:
    """The current outage, or None if there isn't a fresh one.

    Freshness is the safety property: a recorded outage gates dispatch only
    while it has been seen within :data:`OUTAGE_TTL`. Past that the gate opens
    and the next dispatch acts as the probe — which, with detection in place,
    fails in seconds instead of hours. A flag that gated forever would take the
    whole scheduler down on a stale file.
    """
    state = read_state()
    if not state:
        return None
    try:
        seen = datetime.fromisoformat(state["last_seen"])
    except (KeyError, TypeError, ValueError):
        return None
    if (now or _now()) - seen > OUTAGE_TTL:
        return None
    return state


def record_outage(detail: dict, source: str = "ensure") -> dict:
    """Record (or refresh) the machine-wide outage and escalate once.

    ``detected_at`` is carried forward across refreshes so the operator can see
    how long the outage has run; refreshing it each sighting would make a
    four-hour outage read as seconds old — the same defect #905 fixed on
    ``detected_at`` in the prompt sweep.
    """
    prior = read_state() or {}
    now = _now()
    sessions = list(prior.get("sessions") or [])
    if detail.get("session") and detail["session"] not in sessions:
        sessions.append(detail["session"])
    state = {
        "detected_at": prior.get("detected_at") or now.isoformat(),
        "last_seen": now.isoformat(),
        "sessions": sessions,
        "transcript": detail.get("transcript"),
        "source": source,
        "host": socket.gethostname(),
        "escalated_at": prior.get("escalated_at"),
    }
    state["escalated_at"] = _escalate(state, prior)
    write_state(state)
    log_event("outage_detected", session=detail.get("session"),
              transcript=detail.get("transcript"), source=source)
    return state


def _escalate(state: dict, prior: dict) -> str | None:
    """Email the owner once per :data:`ESCALATE_TTL` while the outage persists.

    Best-effort in the strong sense: a missing key or a provider failure must
    never turn "we detected the outage and failed the task fast" into an
    exception that fails it slowly instead. The outage state is written either
    way, so the gate works with or without the email.

    Consequence worth naming: only a SUCCESSFUL send stamps ``escalated_at``,
    so a persistently broken sender is retried once per detection rather than
    once per :data:`ESCALATE_TTL`. That is the intended trade — a send that
    silently counted as delivered would lose the escalation entirely, and
    losing it is strictly worse than retrying it. The retry is cheap (the
    email path is already best-effort and off the critical path) and it stops
    the moment one send lands.
    """
    previous = prior.get("escalated_at")
    if previous:
        try:
            if _now() - datetime.fromisoformat(previous) < ESCALATE_TTL:
                return previous
        except (TypeError, ValueError):
            pass
    try:
        from .channels.email import send_email

        sessions = ", ".join(state.get("sessions") or []) or "(none recorded)"
        body = "\n".join([
            f"Claude Code on `{state.get('host')}` is refusing every turn with "
            f"**{RENDERED}** (`error: {AUTH_ERROR}`).",
            "",
            f"- **First seen:** {state.get('detected_at')}",
            f"- **Sessions affected so far:** {sessions}",
            f"- **Evidence:** {state.get('transcript')}",
            "",
            "Scheduled dispatches are being skipped rather than each burning its "
            "own timeout. Run `/login` in any Claude Code session to clear it; "
            "the gate re-probes automatically and reopens on the first "
            "successful turn.",
        ])
        result = send_email(
            subject=f"[agentwire] Claude login expired on {state.get('host')} — dispatch gated",
            body=body,
        )
        if getattr(result, "success", False):
            log_event("escalated", sessions=state.get("sessions"))
            return _now().isoformat()
        log_event("escalate_failed", error=getattr(result, "error", None))
    except Exception as exc:  # never break the caller
        log_event("escalate_failed", error=str(exc))
    return previous


def check_and_flag(
    session: str, project_path=None, since: float | None = None, source: str = "ensure"
) -> dict | None:
    """The fast-path probe for polling loops. Mirrors ``usage_limit.check_and_park``.

    Cheap when nothing is wrong (one or two bounded tail reads), records the
    machine-wide outage and escalates when it is. Returns the detail dict so
    the caller can name the transcript in its own failure message.
    """
    detail = detect(session, project_path=project_path, since=since)
    if detail is None:
        return None
    record_outage(detail, source=source)
    return detail


def summary_line(detail: dict | None = None) -> str:
    """The operator-facing reason string. Names the cause, not the symptom."""
    where = f" (evidence: {detail['transcript']})" if detail and detail.get("transcript") else ""
    return (
        f"Claude login expired — the agent's turn was refused with "
        f"'{RENDERED}' (error: {AUTH_ERROR}); no completion signal can arrive "
        f"until `/login` is run{where}"
    )
