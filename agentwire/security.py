"""Portal security: Origin validation (CSRF guard) and per-device bearer auth.

Two layers, both enforced by a single aiohttp middleware:

1. Origin check — on every state-changing request (POST/PUT/DELETE/PATCH) and
   WebSocket upgrade, a present ``Origin`` header must match the portal's own
   origin, a localhost equivalent, or an entry in ``server.allowed_origins``.
   Absent Origin is allowed (curl/CLI/scripts don't send one). Always on.

2. Token auth — when auth is configured, every request outside the public
   bootstrap surface (``GET /``, ``/mobile``, ``/pair``, ``/health``,
   ``/static/*``, ``POST /api/pair``) must carry a credential:
   ``Authorization: Bearer <token>`` on HTTP, or a
   ``Sec-WebSocket-Protocol: agentwire.bearer.<token>`` subprotocol on WS
   upgrades. The presented token resolves to a *device* — either the bootstrap
   token (``~/.agentwire/portal.token`` / ``server.auth_token``, used by the
   CLI/MCP/hooks) or a paired device in the registry (``devices.json``; see
   :mod:`agentwire.devices`). Unknown or revoked → 401. Every credential is
   full-access; per-device identity buys named, individually-revocable
   attribution, not capability scoping.

``server.auth_token`` semantics: ``None`` → use the token file; ``""`` →
disable auth (loopback binds only); any other string → explicit override.
"""

import hmac
import ipaddress
import logging
import re
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yaml
from aiohttp import web

from . import devices as devices_mod
from .devices import BOOTSTRAP_DEVICE, Device

logger = logging.getLogger(__name__)

MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
TOKEN_FILE = Path.home() / ".agentwire" / "portal.token"
WS_PROTOCOL_PREFIX = "agentwire.bearer."
_LOCALHOST_NAMES = {"localhost", "127.0.0.1", "::1"}

# Config keys that the portal API must never be able to change — editing them is
# host-file-only (#425). A leaked/compromised token must not be able to disable
# its own auth, rewrite the executables/services that run as RCE, move the bind
# host, or turn off the rm-rf damage-control rules.
FROZEN_CONFIG_KEYS = (
    "server.auth_token",
    "server.host",
    "executables",
    "services",
    "safety",
)
REDACTION_MARKER = "[REDACTED]"
_REDACTED_FIELDS = ("api_key", "auth_token")


# ---------------------------------------------------------------------------
# Token lifecycle


def generate_token() -> str:
    """Generate a new portal auth token (32 bytes, urlsafe)."""
    return secrets.token_urlsafe(32)


def read_token_file() -> Optional[str]:
    """Read the token file. None if missing or empty."""
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return token or None


def write_token_file(token: str) -> None:
    """Write the token file with owner-only permissions."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)


def resolve_auth_token(config) -> Optional[str]:
    """Effective token: config override wins, else the token file.

    ``server.auth_token`` semantics: None = use token file, "" = auth
    disabled, anything else = explicit override.
    """
    if config.server.auth_token is not None:
        return config.server.auth_token or None
    return read_token_file()


def ensure_auth_token(config) -> Optional[str]:
    """Resolve the token, auto-generating the token file when nothing is set.

    Returns None only when auth is explicitly disabled (auth_token: "").
    """
    if config.server.auth_token == "":
        return None
    token = resolve_auth_token(config)
    if token is None:
        token = generate_token()
        write_token_file(token)
        logger.info("Generated portal auth token at %s", TOKEN_FILE)
    return token


def get_local_portal_token() -> Optional[str]:
    """Token for local callers (CLI, MCP, daemons, hooks) hitting the portal.

    Reads ``server.auth_token`` from ~/.agentwire/config.yaml if set,
    else the token file. Standalone so raw-dict config consumers can use it.
    """
    config_path = Path.home() / ".agentwire" / "config.yaml"
    try:
        data = yaml.safe_load(config_path.read_text()) or {}
        override = data.get("server", {}).get("auth_token")
        if override is not None:
            return str(override) or None
    except OSError:
        pass
    return read_token_file()


# ---------------------------------------------------------------------------
# Bind / startup policy


def is_loopback_host(host: str) -> bool:
    """True when the bind host only accepts local connections."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Hostnames and anything unparseable: assume reachable from outside.
        return False


def validate_startup_security(config) -> None:
    """Refuse non-loopback binds without an auth token.

    Call after ``ensure_auth_token()`` has populated config.server.auth_token.
    """
    if not is_loopback_host(config.server.host) and not config.server.auth_token:
        raise SystemExit(
            f"Refusing to start: server.host is {config.server.host!r} (reachable "
            "from the network) but portal auth is disabled (server.auth_token: \"\" "
            "in config). Either remove auth_token from config to use the generated "
            "token, run `agentwire portal token --rotate` to create one, or bind "
            "to 127.0.0.1."
        )


# ---------------------------------------------------------------------------
# Request checks


def _is_websocket_upgrade(request: web.Request) -> bool:
    return request.headers.get("Upgrade", "").lower() == "websocket"


def _effective_port(scheme: str, netloc_port: Optional[int]) -> int:
    if netloc_port:
        return netloc_port
    return 443 if scheme in ("https", "wss") else 80


def origin_allowed(origin: str, request: web.Request, allowed_origins: list) -> bool:
    """Whether a browser Origin may make state-changing requests."""
    if origin in allowed_origins:
        return True
    # The portal's own origin (request.host includes the port when non-default)
    if origin == f"{request.scheme}://{request.host}":
        return True
    # Localhost equivalents on the same effective port
    parsed = urlsplit(origin)
    own = urlsplit(f"{request.scheme}://{request.host}")
    return (
        parsed.hostname in _LOCALHOST_NAMES
        and own.hostname in _LOCALHOST_NAMES
        and _effective_port(parsed.scheme, parsed.port)
        == _effective_port(request.scheme, own.port)
    )


def _is_public_path(request: web.Request) -> bool:
    """The unauthenticated bootstrap surface: page shells, health, pairing.

    ``POST /api/pair`` is public because an unpaired device has no token yet — it
    is instead gated by the short-lived pairing code it must present.
    """
    path = request.path
    if request.method == "POST":
        return path == "/api/pair"
    if request.method != "GET":
        return False
    return path in ("/", "/mobile", "/pair", "/health") or path.startswith("/static/")


# ---------------------------------------------------------------------------
# Frozen security-critical config (#425)


def _dig(data, dotted: str):
    """Walk a dotted path into a nested dict; None if any segment is missing."""
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _load_yaml(text: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


_REDACTED_LINE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<field>[A-Za-z0-9_]+)(?P<sep>[ \t]*:[ \t]*)'
    r'["\']?\[REDACTED\]["\']?[ \t]*(?P<eol>\r?\n?)$'
)
_MAPPING_KEY = re.compile(r'^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_]+)[ \t]*:[ \t]*(?P<val>.*?)[ \t]*\r?\n?$')


def restore_redactions(new_content: str, old_content: str) -> str:
    """Reverse the read-side secret redaction before a config save.

    ``GET /api/config`` replaces ``auth_token``/``api_key`` values with
    ``"[REDACTED]"``. Saving that text back verbatim would overwrite the real
    secret on disk (and falsely trip the frozen-key check on
    ``server.auth_token``).

    Restores each redacted field to the on-disk value **at its own YAML path** —
    so multiple ``api_key`` entries (e.g. several pi providers) each get their
    own secret back, not a single global first-match. Operates on the raw text so
    comments and formatting survive the round-trip; only redacted scalar lines
    change.
    """
    old = _load_yaml(old_content)
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, key) path to the current mapping

    for line in new_content.splitlines(keepends=True):
        key_m = _MAPPING_KEY.match(line)
        if key_m:
            indent = len(key_m.group("indent").expandtabs())
            # Pop siblings/deeper levels so the path reflects this key's parent.
            while stack and stack[-1][0] >= indent:
                stack.pop()
            red_m = _REDACTED_LINE.match(line)
            if red_m and red_m.group("field") in _REDACTED_FIELDS:
                path = ".".join([k for _, k in stack] + [red_m.group("field")])
                real = _dig(old, path)
                if real is not None:
                    out.append(
                        f'{red_m.group("indent")}{red_m.group("field")}'
                        f'{red_m.group("sep")}{_yaml_scalar(real)}{red_m.group("eol")}'
                    )
                    continue  # scalar leaf — don't push onto the path
            # A key that opens a nested mapping (no inline value) extends the path.
            if key_m.group("val") == "":
                stack.append((indent, key_m.group("key")))
        out.append(line)

    return "".join(out)


def _yaml_scalar(value) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def frozen_config_violations(new_content: str, old_content: str) -> list[str]:
    """Frozen keys whose value differs between the submitted and on-disk config.

    Run *after* :func:`restore_redactions` so a redacted-but-unchanged
    ``auth_token`` doesn't read as a change. An empty list means the save is
    allowed.
    """
    new = _load_yaml(new_content)
    old = _load_yaml(old_content)
    return [key for key in FROZEN_CONFIG_KEYS if _dig(new, key) != _dig(old, key)]


def _extract_token(request: web.Request, is_ws: bool) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):]
    if is_ws:
        for proto in request.headers.get("Sec-WebSocket-Protocol", "").split(","):
            proto = proto.strip()
            if proto.startswith(WS_PROTOCOL_PREFIX):
                return proto[len(WS_PROTOCOL_PREFIX):]
    return None


def resolve_device(provided: Optional[str], auth_token: Optional[str]) -> Optional[Device]:
    """Resolve a presented token to a device, or None.

    The bootstrap token (``portal.token`` / config override) maps to a synthetic
    full-scope ``host`` device; everything else is looked up (by hash) in the
    device registry. Revoked devices resolve to None.
    """
    if not provided:
        return None
    if auth_token and hmac.compare_digest(provided.encode(), auth_token.encode()):
        return BOOTSTRAP_DEVICE
    return devices_mod.load_registry_cached().resolve(provided)


def create_security_middleware(auth_token: Optional[str], allowed_origins: list):
    """Build the aiohttp middleware enforcing origin + per-device token auth.

    ``auth_token`` is the bootstrap credential. Auth is enforced whenever it is
    set *or* the registry holds an active paired device; a loopback dev portal
    with neither configured stays open (origin checks still cover the browser).
    """

    @web.middleware
    async def security_middleware(request: web.Request, handler):
        is_ws = _is_websocket_upgrade(request)

        # Origin check: browser CSRF guard on mutations + WS upgrades.
        if request.method in MUTATING_METHODS or is_ws:
            origin = request.headers.get("Origin")
            if origin and not origin_allowed(origin, request, allowed_origins):
                logger.warning(
                    "Rejected %s %s from disallowed origin %s",
                    request.method, request.path, origin,
                )
                raise web.HTTPForbidden(text="Origin not allowed")

        auth_enabled = bool(auth_token) or bool(
            devices_mod.load_registry_cached().active()
        )
        if auth_enabled and not _is_public_path(request):
            provided = _extract_token(request, is_ws)
            device = resolve_device(provided, auth_token)
            if device is None:
                raise web.HTTPUnauthorized(
                    text="Missing or invalid auth token",
                    headers={"WWW-Authenticate": 'Bearer realm="agentwire"'},
                )
            request["device"] = device
            if device.id != BOOTSTRAP_DEVICE.id:
                devices_mod.load_registry_cached().touch(device.id)

        return await handler(request)

    return security_middleware
