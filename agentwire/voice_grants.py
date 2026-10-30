"""Standing approvals for the voice assistant (#1198): "yes, approve those for today".

A small deterministic store, not model memory: a model reads dialog text,
which can contain untrusted content, so it must never be what decides "this is
covered". The owner's spoken approval (judged by ``voice_cli.classify_reply``)
supplies only a SCOPE; everything else comes from the damage-control hook's
own record of the pending ask (``safety._core.voice_pending_ask_path``):

- **keyed on the rule id** the hook matched — a grant covers that rule and no
  other;
- **only grantable asks**: unverifiable (verb-concealing) commands and
  scan-budget overruns are recorded never-grantable by the hook itself;
- **scoped to agentwire-voice**: other sessions never ask through this path
  (the hook's voice marker is set on that one session) and the store names it;
- **always expires**: an hour, the end of today, or the end of this run of the
  session (its next launch invalidates it);
- **logged**: every create / use / clear appends to ``grants-log.jsonl``.

A covered dialog is still a dialog: the voice layer presses the plain one-time
Yes for it (``voice_cli.cmd_voice_approval``), exactly as if the owner had said
yes, and says so out loud.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from .voice_hooks import SESSION

SCOPES = ("hour", "today", "session")

#: How old the hook's pending-ask record may be and still describe the dialog
#: on screen. A dialog waits for an answer; a stale record must not cover a
#: newer, different one.
PENDING_MAX_AGE_S = 15 * 60


def _dir() -> Path:
    """The voice state dir under the account's REAL home.

    Resolved from the password database, never ``$HOME`` / ``$AGENTWIRE_DIR``:
    this CLI can be run by an agent, and an env-redirected store would let
    ``HOME=/tmp/x agentwire voice approval`` press Yes on a real dialog with a
    fake grant. The real files are damage-control protected against tool
    writes (``~/.agentwire/voice-approvals``)."""
    import pwd

    from .safety._core import VOICE_APPROVALS_DIR

    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".agentwire" / VOICE_APPROVALS_DIR


def grants_path() -> Path:
    return _dir() / "grants.json"


def log_path() -> Path:
    return _dir() / "grants-log.jsonl"


def log(event: str, **fields) -> None:
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        with log_path().open("a") as f:
            f.write(json.dumps({"ts": time.time(), "event": event, **fields}) + "\n")
    except OSError:
        pass


def _session_launch() -> str:
    from .core import load_session_metadata

    return str(load_session_metadata(SESSION).get("launched_at") or "")


def expiry(scope: str, now: float) -> float:
    """An hour from now, or the end of today. A "session" grant also ends at
    the session's next launch (see :func:`_valid`), and never outlives today."""
    if scope == "hour":
        return now + 3600
    return datetime.fromtimestamp(now).replace(hour=23, minute=59, second=59, microsecond=0).timestamp()


def _load() -> list[dict]:
    try:
        rows = json.loads(grants_path().read_text())
    except (OSError, ValueError):
        return []
    return [g for g in rows if isinstance(g, dict)] if isinstance(rows, list) else []


def _save(grants: list[dict]) -> None:
    grants_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = grants_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(grants, indent=1))
    tmp.replace(grants_path())


def _valid(g: dict, now: float, launch: str) -> bool:
    if g.get("session") != SESSION or not g.get("rule_id"):
        return False
    if float(g.get("expires_at") or 0) <= now:
        return False
    return g.get("scope") != "session" or g.get("session_launch") == launch


def active(now: float | None = None) -> list[dict]:
    """Unexpired grants; expired ones are pruned (and logged) on the way."""
    now = time.time() if now is None else now
    launch = _session_launch()
    grants = _load()
    keep = [g for g in grants if _valid(g, now, launch)]
    if len(keep) != len(grants):
        for g in grants:
            if g not in keep:
                log("expired", rule_id=g.get("rule_id"), scope=g.get("scope"))
        _save(keep)
    return keep


def pending_ask(now: float | None = None) -> dict | None:
    """The hook's record of the ask now on screen, if fresh."""
    from .safety._core import VOICE_PENDING_ASK_FILE

    now = time.time() if now is None else now
    try:
        rec = json.loads((_dir() / VOICE_PENDING_ASK_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or now - float(rec.get("ts") or 0) > PENDING_MAX_AGE_S:
        return None
    return rec


def add(rule_id: str, scope: str, now: float | None = None) -> dict:
    """Grant *rule_id* for *scope*. Replaces an existing grant for that rule."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    now = time.time() if now is None else now
    grant = {"rule_id": rule_id, "session": SESSION, "scope": scope,
             "created_at": now, "expires_at": expiry(scope, now),
             "session_launch": _session_launch()}
    grants = [g for g in active(now) if g.get("rule_id") != rule_id] + [grant]
    _save(grants)
    log("created", rule_id=rule_id, scope=scope, expires_at=grant["expires_at"])
    return grant


def covering(rule_id: str | None, now: float | None = None) -> dict | None:
    """The active grant for *rule_id*, if any."""
    if not rule_id:
        return None
    return next((g for g in active(now) if g.get("rule_id") == rule_id), None)


def clear(rule_id: str | None = None) -> int:
    """Remove one rule's grant, or all. Returns how many were removed."""
    grants = _load()
    keep = [g for g in grants if rule_id is not None and g.get("rule_id") != rule_id]
    removed = len(grants) - len(keep)
    _save(keep)
    if removed:
        log("cleared", rule_id=rule_id or "*", count=removed)
    return removed


SCOPE_PHRASES = {
    "hour": "for the next hour",
    "today": "for the rest of today",
    "session": "until I restart",
}
