"""Durable per-run traces for the buddy (#1191).

#1190 stalled right after the owner said "Confirm Bravo" — no tool call, no
reply, the page stopped listening — and there was nothing to read: the bridge's
stdout was empty and the page's log lived in the DOM, gone on reload. A voice
channel the owner cannot see into has to be diagnosable AFTER the fact, by an
agent, without the owner reproducing it. This is that record.

One JSONL file per bridge run under ``~/.agentwire/logs/buddy/<run-id>.jsonl``.
Every line carries ``ts`` (epoch ms), ``run``, ``seq`` (bridge-assigned, so the
file has one total order across both sources), ``source`` (``client`` |
``bridge``), ``kind`` and ``fields``. The page batches its events to ``/trace``;
the bridge records its own (tool calls with duration and outcome, mint, anchor,
HTTP errors) straight into the same file.

**Never in the way.** :meth:`TraceLog.record` never raises — a full disk or a
bad field costs the trace line, never the owner's live call. Size-capped per
run (one ``trace.capped`` marker, then drops), oldest runs pruned on open.

**Redaction is structural, not a promise.** :func:`redact` runs on every field
before it is written: a key that names a credential or audio is replaced
outright, values shaped like an OpenAI key or ephemeral secret are replaced
wherever they appear, ``untrusted_content`` (GitHub/project text written by
strangers) is cut to a short marked preview, and every long string is
truncated and says so.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path

from ..core import CONFIG_DIR

#: Where runs live. A module constant (not a default argument) so the test
#: harness's home relocation reaches it.
TRACE_DIR = CONFIG_DIR / "logs" / "buddy"

#: Per-run cap. A long session at a few events per second is a few MB; past
#: this something is looping, and the first 8 MB of a loop is the diagnosis.
MAX_RUN_BYTES = 8 * 1024 * 1024
#: Runs kept on disk. One per bridge start — weeks of normal use.
KEEP_RUNS = 30

MAX_STR = 400
UNTRUSTED_PREVIEW = 120
_MAX_DEPTH = 6
_MAX_ITEMS = 50

#: Keys whose VALUE is never written. "key"/"token"/"secret" cover the API key,
#: the run token, the client secret and the spine's confirm_token; "audio" and
#: "sdp" cover the media payloads.
_SECRET_KEY_RX = re.compile(
    r"(token|secret|api_?key|^key$|authorization|password|bearer|audio|sdp|delta)",
    re.IGNORECASE,
)
#: Credential-shaped VALUES, wherever they appear (a pasted error body, a log
#: line that interpolated one).
_SECRET_VALUE_RX = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ek_[A-Za-z0-9_-]{8,}|Bearer\s+\S+)")

#: A credential-named key INSIDE a string — the page's log line for a tool call
#: is ``send_session_message {"confirm_token":"…"}``, one string, so the
#: key-based redaction above never sees the key.
_EMBEDDED_SECRET_RX = re.compile(
    r'("[A-Za-z_]*(?:token|secret|api_?key|authorization|password)[A-Za-z_]*"\s*:\s*)"[^"]*"',
    re.IGNORECASE,
)

REDACTED = "[redacted]"


def _truncate(text: str, limit: int = MAX_STR) -> str:
    text = _SECRET_VALUE_RX.sub(REDACTED, text)
    text = _EMBEDDED_SECRET_RX.sub(r'\1"' + REDACTED + '"', text)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[truncated {len(text) - limit} chars]"


def redact(value, depth: int = 0):
    """A JSON-safe, credential-free, bounded copy of *value*."""
    if depth > _MAX_DEPTH:
        return "[too deep]"
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                out["…"] = f"[{len(value) - _MAX_ITEMS} more keys]"
                break
            k = str(k)
            if _SECRET_KEY_RX.search(k):
                out[k] = REDACTED
            elif k == "untrusted_content":
                out[k] = "[untrusted] " + _truncate(
                    json.dumps(v, default=str), UNTRUSTED_PREVIEW
                )
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [redact(v, depth + 1) for v in value[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            items.append(f"[{len(value) - _MAX_ITEMS} more]")
        return items
    if isinstance(value, str):
        return _truncate(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _truncate(str(value))


def new_run_id(now: float | None = None) -> str:
    """Sortable by start time, unique within a second."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    return f"{stamp}-{secrets.token_hex(3)}"


class TraceLog:
    """The append-only trace for one bridge run. Thread-safe; never raises."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        run_id: str | None = None,
        max_bytes: int = MAX_RUN_BYTES,
        keep_runs: int = KEEP_RUNS,
        clock=time.time,
    ):
        self.directory = Path(directory) if directory else TRACE_DIR
        self.run_id = run_id or new_run_id()
        self.path = self.directory / f"{self.run_id}.jsonl"
        self.max_bytes = max_bytes
        self.keep_runs = keep_runs
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self._bytes = 0
        self._capped = False
        self._opened = False

    def _open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        old = sorted(self.directory.glob("*.jsonl"))
        for stale in old[: max(0, len(old) - (self.keep_runs - 1))]:
            stale.unlink(missing_ok=True)
        self._opened = True

    def record(self, source: str, kind: str, fields: dict | None = None,
               ts: int | None = None) -> bool:
        """Append one line. Returns whether it was written; never raises."""
        try:
            with self._lock:
                if self._capped:
                    return False
                if not self._opened:
                    self._open()
                self._seq += 1
                line = {
                    "ts": ts if isinstance(ts, int) and not isinstance(ts, bool)
                    else int(self._clock() * 1000),
                    "run": self.run_id,
                    "seq": self._seq,
                    "source": source,
                    "kind": _truncate(str(kind), 80),
                    "fields": redact(fields or {}),
                }
                data = json.dumps(line, default=str) + "\n"
                if self._bytes + len(data) > self.max_bytes:
                    self._capped = True
                    data = json.dumps({
                        "ts": int(self._clock() * 1000), "run": self.run_id,
                        "seq": self._seq, "source": "bridge", "kind": "trace.capped",
                        "fields": {"max_bytes": self.max_bytes},
                    }) + "\n"
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(data)
                self._bytes += len(data)
                return not self._capped
        except Exception:  # noqa: BLE001  # a trace line is never worth a live call
            return False

    def ingest(self, payload) -> int:
        """Record a client batch: ``{"events": [{"ts", "kind", "fields"}…]}``.

        Returns how many were written. Malformed entries are skipped, not
        fatal — the page drops on error too.
        """
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            return 0
        written = 0
        for event in events[:500]:
            if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
                continue
            fields = event.get("fields")
            if self.record("client", event["kind"],
                           fields if isinstance(fields, dict) else {},
                           ts=event.get("ts")):
                written += 1
        return written


# ---------------------------------------------------------------------------
# Reading — the CLI side
# ---------------------------------------------------------------------------

#: The kinds ``buddy log --stalls`` selects: the watchdog's verdict, and the
#: crash-shaped events that end a conversation without one.
STALL_KINDS = frozenset({"stall", "window.error", "unhandledrejection", "dc.close", "pc.failed"})


def list_runs(directory: Path | None = None) -> list[Path]:
    """Runs newest-first (run ids sort by start time)."""
    d = Path(directory) if directory else TRACE_DIR
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), reverse=True)


def resolve_run(spec: str = "latest", directory: Path | None = None) -> Path | None:
    runs = list_runs(directory)
    if not runs:
        return None
    if spec in ("", "latest"):
        return runs[0]
    for run in runs:
        if run.stem == spec or run.stem.startswith(spec):
            return run
    return None


def read_events(path: Path) -> list[dict]:
    """Every parseable line of *path*; a torn last line is skipped."""
    out = []
    try:
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    out.append(event)
    except OSError:
        return []
    return out


def format_event(event: dict) -> str:
    """One human-readable line."""
    ts = event.get("ts") or 0
    clock = time.strftime("%H:%M:%S", time.localtime(ts / 1000)) + f".{ts % 1000:03d}"
    fields = event.get("fields") or {}
    detail = " ".join(f"{k}={json.dumps(v, default=str)}" for k, v in fields.items())
    src = "C" if event.get("source") == "client" else "B"
    return f"{clock} {src} {event.get('kind', '?')} {detail}".rstrip()
