"""Per-device portal credentials: a hashed device registry + pairing flow.

The portal's bootstrap credential is still ``~/.agentwire/portal.token`` (the
host/owner's full-scope token, used by the CLI, MCP server, hooks and daemons —
see :func:`agentwire.security.get_local_portal_token`). What this module adds is
*additional, individually-revocable* device credentials so a phone that only does
push-to-talk no longer has to hold the same god-token as the laptop.

Two files under ``~/.agentwire/`` (both 0600):

* ``devices.json`` — the registry. One entry per paired device::

      { "id", "name", "token_hash", "scope", "session",
        "created", "last_seen", "revoked" }

  Only the **sha256 hash** of each device token is stored; the plaintext is shown
  once at pairing time and never persisted.

* ``pairings.json`` — short-lived pending pairing codes. ``agentwire portal pair``
  (host process) writes one; the portal's ``POST /api/pair`` (server process)
  consumes it and mints the device token. File-backed so the two processes share.

Every credential is full-access; the win over the old single shared token is that
each device is *named, individually revocable, and attributable* — revoking one
phone no longer logs out the laptop.
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

try:
    import fcntl  # POSIX only — agentwire targets macOS/Linux.
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

DEVICES_FILE = Path.home() / ".agentwire" / "devices.json"
PAIRINGS_FILE = Path.home() / ".agentwire" / "pairings.json"

PAIRING_TTL_SECONDS = 600  # pairing codes expire after 10 minutes
_LAST_SEEN_THROTTLE = 60  # at most one last_seen write per device per minute

# Crockford-ish alphabet — no ambiguous 0/O/1/I/L.
_PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


# ---------------------------------------------------------------------------
# Primitives


def hash_token(token: str) -> str:
    """Stable sha256 hex digest of a device token (what the registry stores)."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_device_token() -> str:
    """A fresh device token (32 bytes, urlsafe) — same strength as the bootstrap."""
    return secrets.token_urlsafe(32)


def generate_device_id() -> str:
    return "dev_" + secrets.token_hex(4)


def generate_pairing_code() -> str:
    return "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(8))


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else _now()))


def _parse_iso(value: Optional[str]) -> float:
    """Parse a stored UTC ISO stamp back to epoch seconds (0 on failure)."""
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Concurrency-safe persistence
#
# Both files (devices.json, pairings.json) are shared across processes — the
# portal serves auth on every request while the CLI revokes/pairs out of band.
# Every read-modify-write therefore runs under an exclusive flock on a sibling
# ``.lock`` file (a dedicated lock target, so the atomic os.replace below never
# swaps the inode we're holding), and writes go through a temp-file + rename so
# a reader never sees a half-written file.


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


# ---------------------------------------------------------------------------
# Device registry


@dataclass
class Device:
    id: str
    name: str
    token_hash: str
    created: str = ""
    last_seen: Optional[str] = None
    revoked: bool = False

    def public(self) -> dict:
        """Registry entry without the token hash — safe to hand to the UI/CLI."""
        d = asdict(self)
        d.pop("token_hash", None)
        return d


# A synthetic device for the bootstrap token (portal.token / config override).
# It never lives in the registry — revoking it means rotating the token file
# (`agentwire portal token --rotate`).
BOOTSTRAP_DEVICE = Device(id="host", name="host (bootstrap token)", token_hash="")


def _read_devices(path: Path) -> list[Device]:
    """Parse the on-disk registry into Device rows (authoritative current state)."""
    devices: list[Device] = []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, dict):
        for entry in raw.get("devices", []):
            if not isinstance(entry, dict) or "token_hash" not in entry:
                continue
            devices.append(
                Device(
                    id=entry.get("id", ""),
                    name=entry.get("name", ""),
                    token_hash=entry["token_hash"],
                    created=entry.get("created", ""),
                    last_seen=entry.get("last_seen"),
                    revoked=bool(entry.get("revoked", False)),
                )
            )
    return devices


class DeviceRegistry:
    """Load/save the device registry and resolve presented tokens to devices.

    Reads (``resolve``/``active``) use the in-memory snapshot from ``load``.
    **Writes (``add``/``revoke``/``touch``) re-read the file fresh under an
    exclusive lock and write authoritatively** — never persisting a stale
    snapshot. This is what makes revoke a real kill-switch: a concurrent
    ``touch`` can't resurrect a device revoked between load and write.
    """

    def __init__(self, path: Path, devices: Optional[list[Device]] = None):
        self.path = path
        self.devices: list[Device] = devices or []

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "DeviceRegistry":
        path = path or DEVICES_FILE
        return cls(path, _read_devices(path))

    def save(self) -> None:
        """Authoritatively persist the in-memory snapshot (locked, atomic)."""
        with _file_lock(self.path):
            _atomic_write_json(self.path, {"devices": [asdict(d) for d in self.devices]})

    # -- mutation (re-read fresh under lock, write authoritatively) --------

    def add(self, name: str, token: Optional[str] = None) -> tuple[Device, str]:
        """Register a new device, returning (device, plaintext_token).

        The plaintext is the only time the token exists on the host — store the
        hash, hand the caller the secret.
        """
        token = token or generate_device_token()
        device = Device(
            id=generate_device_id(),
            name=name or "device",
            token_hash=hash_token(token),
            created=_iso(),
        )
        with _file_lock(self.path):
            devices = _read_devices(self.path)
            devices.append(device)
            _atomic_write_json(self.path, {"devices": [asdict(d) for d in devices]})
            self.devices = devices
        return device, token

    def revoke(self, device_id: str) -> bool:
        with _file_lock(self.path):
            devices = _read_devices(self.path)
            found = False
            for d in devices:
                if d.id == device_id and not d.revoked:
                    d.revoked = True
                    found = True
            if found:
                _atomic_write_json(self.path, {"devices": [asdict(d) for d in devices]})
            self.devices = devices
        return found

    def touch(self, device_id: str) -> None:
        """Best-effort last_seen update, throttled to one write per minute.

        Re-reads under the lock and refuses to write a device that is absent or
        already revoked — so it can never undo a concurrent revoke.
        """
        now = _now()
        with _file_lock(self.path):
            devices = _read_devices(self.path)
            target = next((d for d in devices if d.id == device_id), None)
            if target is None or target.revoked:
                return
            if now - _parse_iso(target.last_seen) < _LAST_SEEN_THROTTLE:
                return
            target.last_seen = _iso(now)
            try:
                _atomic_write_json(self.path, {"devices": [asdict(d) for d in devices]})
                self.devices = devices
            except OSError:
                pass

    # -- lookup -----------------------------------------------------------

    def resolve(self, token: str) -> Optional[Device]:
        """Hash the presented token and return the matching live device, if any."""
        if not token:
            return None
        presented = hash_token(token)
        for d in self.devices:
            if d.revoked:
                continue
            if hmac.compare_digest(presented, d.token_hash):
                return d
        return None

    def active(self) -> list[Device]:
        return [d for d in self.devices if not d.revoked]


# Cache the registry by file mtime so the security middleware doesn't reparse
# JSON on every request. A revoke/add/touch rewrites the file → mtime changes →
# the next read reparses, so revocation is effective immediately.
_cache: dict[str, tuple[Optional[float], DeviceRegistry]] = {}


def load_registry_cached(path: Optional[Path] = None) -> DeviceRegistry:
    path = path or DEVICES_FILE
    try:
        mtime: Optional[float] = path.stat().st_mtime
    except OSError:
        mtime = None
    key = str(path)
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    reg = DeviceRegistry.load(path)
    _cache[key] = (mtime, reg)
    return reg


# ---------------------------------------------------------------------------
# Pending pairings (host writes, portal consumes)


@dataclass
class Pairing:
    code: str
    name: str
    expires: float

    def expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else _now()) > self.expires


def _load_pairings(path: Path) -> list[Pairing]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Pairing] = []
    if isinstance(raw, dict):
        for e in raw.get("pairings", []):
            if not isinstance(e, dict) or "code" not in e:
                continue
            out.append(
                Pairing(
                    code=e["code"],
                    name=e.get("name", "device"),
                    expires=float(e.get("expires", 0)),
                )
            )
    return out


def _save_pairings(path: Path, pairings: list[Pairing]) -> None:
    """Persist pending pairings (locked, atomic). Call under ``_file_lock``."""
    _atomic_write_json(path, {"pairings": [asdict(p) for p in pairings]})


def create_pairing(
    name: str,
    ttl: int = PAIRING_TTL_SECONDS,
    path: Optional[Path] = None,
) -> Pairing:
    """Create and persist a pending pairing code (host side)."""
    path = path or PAIRINGS_FILE
    pairing = Pairing(
        code=generate_pairing_code(),
        name=name or "device",
        expires=_now() + ttl,
    )
    with _file_lock(path):
        pairings = [p for p in _load_pairings(path) if not p.expired()]
        pairings.append(pairing)
        _save_pairings(path, pairings)
    return pairing


def consume_pairing(code: str, path: Optional[Path] = None) -> Optional[Pairing]:
    """Validate a pairing code, removing it (one-shot). Portal side.

    Atomic compare-and-delete under the pairings lock: concurrent redemptions of
    the same code can't all win — exactly one acquires the lock, removes the
    code, and returns it; the rest re-read and find it gone. Expired entries are
    swept on the way through.
    """
    if not code:
        return None
    path = path or PAIRINGS_FILE
    code = code.strip().upper()
    with _file_lock(path):
        pairings = _load_pairings(path)
        match: Optional[Pairing] = None
        survivors: list[Pairing] = []
        now = _now()
        for p in pairings:
            if p.expired(now):
                continue  # drop expired
            if match is None and hmac.compare_digest(p.code, code):
                match = p
                continue  # consume (don't carry forward)
            survivors.append(p)
        _save_pairings(path, survivors)
    return match
