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

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

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


class DeviceRegistry:
    """Load/save the device registry and resolve presented tokens to devices."""

    def __init__(self, path: Path, devices: Optional[list[Device]] = None):
        self.path = path
        self.devices: list[Device] = devices or []

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "DeviceRegistry":
        path = path or DEVICES_FILE
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
        return cls(path, devices)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"devices": [asdict(d) for d in self.devices]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n")
        self.path.chmod(0o600)

    # -- mutation ---------------------------------------------------------

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
        self.devices.append(device)
        self.save()
        return device, token

    def revoke(self, device_id: str) -> bool:
        for d in self.devices:
            if d.id == device_id and not d.revoked:
                d.revoked = True
                self.save()
                return True
        return False

    def touch(self, device_id: str) -> None:
        """Best-effort last_seen update, throttled to one write per minute."""
        for d in self.devices:
            if d.id != device_id:
                continue
            now = _now()
            try:
                prev = time.mktime(time.strptime(d.last_seen, "%Y-%m-%dT%H:%M:%SZ")) if d.last_seen else 0
            except (ValueError, TypeError):
                prev = 0
            if now - prev >= _LAST_SEEN_THROTTLE:
                d.last_seen = _iso(now)
                try:
                    self.save()
                except OSError:
                    pass
            return

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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pairings": [asdict(p) for p in pairings]}, indent=2) + "\n")
    path.chmod(0o600)


def create_pairing(
    name: str,
    ttl: int = PAIRING_TTL_SECONDS,
    path: Optional[Path] = None,
) -> Pairing:
    """Create and persist a pending pairing code (host side)."""
    path = path or PAIRINGS_FILE
    pairings = [p for p in _load_pairings(path) if not p.expired()]
    pairing = Pairing(
        code=generate_pairing_code(),
        name=name or "device",
        expires=_now() + ttl,
    )
    pairings.append(pairing)
    _save_pairings(path, pairings)
    return pairing


def consume_pairing(code: str, path: Optional[Path] = None) -> Optional[Pairing]:
    """Validate a pairing code, removing it (one-shot). Portal side.

    Returns the Pairing on success, None if unknown/expired. Expired entries are
    swept on the way through.
    """
    if not code:
        return None
    path = path or PAIRINGS_FILE
    code = code.strip().upper()
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
