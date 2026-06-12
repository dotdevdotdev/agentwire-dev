"""Portal security: Origin validation (CSRF guard) and bearer-token auth.

Two layers, both enforced by a single aiohttp middleware:

1. Origin check — on every state-changing request (POST/PUT/DELETE/PATCH) and
   WebSocket upgrade, a present ``Origin`` header must match the portal's own
   origin, a localhost equivalent, or an entry in ``server.allowed_origins``.
   Absent Origin is allowed (curl/CLI/scripts don't send one). Always on.

2. Token auth — when an auth token is configured, every request outside the
   public bootstrap surface (``GET /``, ``/health``, ``/static/*``) must carry
   it: ``Authorization: Bearer <token>`` on HTTP, or a
   ``Sec-WebSocket-Protocol: agentwire.bearer.<token>`` subprotocol on
   WebSocket upgrades. Required whenever the bind is non-loopback.

The token lives at ``~/.agentwire/portal.token`` (0600, auto-generated).
``server.auth_token`` in config overrides it: ``""`` disables auth (loopback
binds only), any other string replaces the file token.
"""

import hmac
import ipaddress
import logging
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yaml
from aiohttp import web

logger = logging.getLogger(__name__)

MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
TOKEN_FILE = Path.home() / ".agentwire" / "portal.token"
WS_PROTOCOL_PREFIX = "agentwire.bearer."
_LOCALHOST_NAMES = {"localhost", "127.0.0.1", "::1"}


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
    """The unauthenticated bootstrap surface: the page shells + health check."""
    if request.method != "GET":
        return False
    path = request.path
    return path in ("/", "/mobile", "/health") or path.startswith("/static/")


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


def create_security_middleware(auth_token: Optional[str], allowed_origins: list):
    """Build the aiohttp middleware enforcing origin + token policy."""

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

        # Token check: everything except the public bootstrap surface.
        if auth_token and not _is_public_path(request):
            provided = _extract_token(request, is_ws)
            if not provided or not hmac.compare_digest(
                provided.encode(), auth_token.encode()
            ):
                raise web.HTTPUnauthorized(
                    text="Missing or invalid auth token",
                    headers={"WWW-Authenticate": 'Bearer realm="agentwire"'},
                )

        return await handler(request)

    return security_middleware
