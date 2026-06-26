"""Web Push subscription storage — per-device push endpoints (#483).

A locked, atomic JSON file mirroring :mod:`agentwire.devices`: the portal and the
CLI both touch it out of band, so every read-modify-write runs under an exclusive
flock and writes go through a temp-file + rename.

One file under ``~/.agentwire/`` (0600):

* ``push_subscriptions.json`` — the registry. One entry per browser push
  subscription (a phone/laptop that has granted notification permission and
  registered a service worker)::

      { "endpoint", "keys": {"p256dh", "auth"}, "device", "created", "last_push" }

  ``endpoint`` is the unique key (the push service URL the browser handed us);
  re-subscribing the same browser upserts rather than duplicating. ``device`` is
  the optional device id/name the subscribing client reported, purely for the
  owner's benefit when listing subscriptions.

The subscription object is NOT a secret — it can only be used to push to that one
browser, and only by a server holding the matching VAPID private key. We still
store it 0600 because it identifies the owner's devices.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

try:
    import fcntl  # POSIX only — agentwire targets macOS/Linux.
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

SUBSCRIPTIONS_FILE = Path.home() / ".agentwire" / "push_subscriptions.json"


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else _now()))


@contextmanager
def _file_lock(path: Path):
    """Exclusive cross-process lock keyed on ``<path>.lock``."""
    if fcntl is None:  # pragma: no cover - non-POSIX fallback
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / (path.name + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON via temp-file + atomic rename, owner-only. Call under lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class PushSubscription:
    """A browser Web Push subscription (what the SW handed the page)."""

    endpoint: str
    keys: dict = field(default_factory=dict)  # {"p256dh": ..., "auth": ...}
    device: str = ""
    created: str = ""
    last_push: Optional[str] = None

    def to_webpush(self) -> dict:
        """Shape pywebpush expects for ``subscription_info``."""
        return {"endpoint": self.endpoint, "keys": dict(self.keys)}


def _read(path: Path) -> list[PushSubscription]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out: list[PushSubscription] = []
    if isinstance(raw, dict):
        for e in raw.get("subscriptions", []):
            if not isinstance(e, dict) or not e.get("endpoint"):
                continue
            keys = e.get("keys") or {}
            out.append(
                PushSubscription(
                    endpoint=e["endpoint"],
                    keys=keys if isinstance(keys, dict) else {},
                    device=e.get("device", ""),
                    created=e.get("created", ""),
                    last_push=e.get("last_push"),
                )
            )
    return out


def _write(path: Path, subs: list[PushSubscription]) -> None:
    _atomic_write_json(path, {"subscriptions": [asdict(s) for s in subs]})


def load(path: Optional[Path] = None) -> list[PushSubscription]:
    """Current subscriptions (unlocked read — fine for the send fan-out)."""
    return _read(path or SUBSCRIPTIONS_FILE)


def add(
    endpoint: str,
    keys: dict,
    device: str = "",
    path: Optional[Path] = None,
) -> PushSubscription:
    """Upsert a subscription by endpoint (re-subscribe is idempotent)."""
    path = path or SUBSCRIPTIONS_FILE
    sub = PushSubscription(
        endpoint=endpoint,
        keys=dict(keys or {}),
        device=device or "",
        created=_iso(),
    )
    with _file_lock(path):
        subs = [s for s in _read(path) if s.endpoint != endpoint]
        subs.append(sub)
        _write(path, subs)
    return sub


def remove(endpoint: str, path: Optional[Path] = None) -> bool:
    """Drop a subscription by endpoint. True if one was removed."""
    path = path or SUBSCRIPTIONS_FILE
    with _file_lock(path):
        subs = _read(path)
        kept = [s for s in subs if s.endpoint != endpoint]
        if len(kept) == len(subs):
            return False
        _write(path, kept)
    return True


def prune(endpoints: list[str], path: Optional[Path] = None) -> int:
    """Drop every subscription whose endpoint is in ``endpoints`` (expired/gone).

    Used after a send to garbage-collect subscriptions the push service rejected
    with 404/410. Returns the count removed.
    """
    if not endpoints:
        return 0
    path = path or SUBSCRIPTIONS_FILE
    dead = set(endpoints)
    with _file_lock(path):
        subs = _read(path)
        kept = [s for s in subs if s.endpoint not in dead]
        removed = len(subs) - len(kept)
        if removed:
            _write(path, kept)
    return removed


def touch(endpoint: str, path: Optional[Path] = None) -> None:
    """Best-effort last_push stamp."""
    path = path or SUBSCRIPTIONS_FILE
    with _file_lock(path):
        subs = _read(path)
        target = next((s for s in subs if s.endpoint == endpoint), None)
        if target is None:
            return
        target.last_push = _iso()
        try:
            _write(path, subs)
        except OSError:
            pass
