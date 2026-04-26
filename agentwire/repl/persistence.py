"""Transcript persistence for agentwire REPL sessions.

Every REPL session creates a directory under `~/.agentwire/sessions/repl/`
containing:
  - metadata.json — session-level config + running totals, updated on close
  - transcript.jsonl — one JSON object per line, event stream

Event shape mirrors `agentwire/workflows/storage.py` where possible so the
portal history window can render REPL sessions alongside workflow runs.

We add two REPL-specific event types on top of the workflow vocabulary:
  - `user_input` — the literal text the user typed for a turn
  - `restart` — emitted when /clear resets the SDK conversation
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, IO

from agentwire.repl.state import ReplState


SCHEMA_VERSION = 1
DEFAULT_REPL_HOME = Path.home() / ".agentwire" / "sessions" / "repl"


@dataclass
class Transcript:
    """Handle for an open transcript file + metadata state.

    Hold onto one of these for the life of the REPL session. `/clear` calls
    `record_restart()` to mark the boundary but keeps writing to the same
    file (a session is the REPL's lifetime; restarts are events within it).
    """
    name: str
    session_dir: Path
    events_path: Path
    metadata_path: Path
    _events_file: IO[str] = field(repr=False)
    started_at: float = field(default_factory=time.time)

    def write_event(self, event: dict[str, Any]) -> None:
        payload = {"ts": time.time(), **event}
        self._events_file.write(json.dumps(payload, default=str) + "\n")
        self._events_file.flush()

    def close(self) -> None:
        try:
            self._events_file.close()
        except Exception:
            pass


def _session_name(now: datetime | None = None) -> str:
    """Auto-generate a session name: YYYY-MM-DDTHH-MM-SS-<6 hex>."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def create_session(
    mode: str,
    model: str,
    allowed_tools: list[str],
    name: str | None = None,
    home: Path | None = None,
) -> Transcript:
    """Create a session directory + open the events file for append.

    Writes the initial `metadata.json`. If `name` is given and already exists,
    a numeric suffix is added to avoid clobbering.
    """
    base = home or DEFAULT_REPL_HOME
    base.mkdir(parents=True, exist_ok=True)

    chosen = name or _session_name()
    session_dir = base / chosen
    if session_dir.exists():
        # Avoid overwriting; pick a suffix.
        i = 2
        while (base / f"{chosen}-{i}").exists():
            i += 1
        chosen = f"{chosen}-{i}"
        session_dir = base / chosen

    session_dir.mkdir(parents=True)
    events_path = session_dir / "transcript.jsonl"
    metadata_path = session_dir / "metadata.json"

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "name": chosen,
        "started_at": time.time(),
        "mode": mode,
        "model": model,
        "allowed_tools": list(allowed_tools),
        "sdk_session_ids": [],
        "turn_count": 0,
        "restart_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cost_usd": 0.0,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))

    events_file = events_path.open("a")
    return Transcript(
        name=chosen,
        session_dir=session_dir,
        events_path=events_path,
        metadata_path=metadata_path,
        _events_file=events_file,
        started_at=metadata["started_at"],
    )


def finalize(transcript: Transcript, state: ReplState) -> None:
    """Update metadata.json with final state + ended_at. Idempotent."""
    try:
        current = json.loads(transcript.metadata_path.read_text())
    except Exception:
        current = {}
    current.update({
        "turn_count": state.turn_count,
        "restart_count": state.restart_count,
        "total_input_tokens": state.total_input_tokens,
        "total_output_tokens": state.total_output_tokens,
        "total_cost_usd": state.total_cost_usd,
        "ended_at": time.time(),
    })
    # Append session_id if tracked and not already recorded.
    if state.session_id:
        ids = current.get("sdk_session_ids") or []
        if state.session_id not in ids:
            ids.append(state.session_id)
        current["sdk_session_ids"] = ids
    transcript.metadata_path.write_text(json.dumps(current, indent=2))


def record_session_id(transcript: Transcript, session_id: str | None) -> None:
    """Append an SDK session_id to metadata as soon as we learn it."""
    if not session_id:
        return
    try:
        current = json.loads(transcript.metadata_path.read_text())
    except Exception:
        current = {}
    ids = current.get("sdk_session_ids") or []
    if session_id in ids:
        return
    ids.append(session_id)
    current["sdk_session_ids"] = ids
    transcript.metadata_path.write_text(json.dumps(current, indent=2))


def list_sessions(home: Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent sessions' metadata, newest first."""
    base = home or DEFAULT_REPL_HOME
    if not base.exists():
        return []
    entries: list[dict[str, Any]] = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        meta_path = d / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        entries.append(meta)
    entries.sort(key=lambda m: m.get("started_at", 0), reverse=True)
    return entries[:limit]


def load_session(name: str, home: Path | None = None) -> dict[str, Any] | None:
    """Load metadata.json for a named session. None if missing / corrupt."""
    base = home or DEFAULT_REPL_HOME
    meta = base / name / "metadata.json"
    if not meta.is_file():
        return None
    try:
        return json.loads(meta.read_text())
    except Exception:
        return None


async def tail_transcript(
    name: str,
    home: Path | None = None,
    poll_interval: float = 0.25,
    follow: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Yield transcript events for `name`: existing first, then live appends.

    Reads `transcript.jsonl` line by line as events are appended, the same
    way `tail -f` works for plain text. Yields one parsed dict per line,
    skipping malformed lines. When `follow=False`, returns after replaying
    the current file content (useful for one-shot replay / tests).

    Watch-mode in the portal uses this: a fresh client gets the whole
    history first, then sees new events as they're written. Pane death
    doesn't kill the watch — `transcript.jsonl` is the SSOT.
    """
    base = home or DEFAULT_REPL_HOME
    path = base / name / "transcript.jsonl"

    # Wait for the file to appear if we're following an in-flight session
    # that may still be initializing.
    if follow:
        deadline = time.time() + 5.0
        while not path.is_file() and time.time() < deadline:
            await asyncio.sleep(poll_interval)

    if not path.is_file():
        return

    handle = path.open("r")
    try:
        buf = ""
        while True:
            chunk = handle.read()
            if chunk:
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
                continue
            if not follow:
                # Drained — done.
                if buf.strip():
                    try:
                        yield json.loads(buf.strip())
                    except json.JSONDecodeError:
                        pass
                return
            await asyncio.sleep(poll_interval)
    finally:
        handle.close()
