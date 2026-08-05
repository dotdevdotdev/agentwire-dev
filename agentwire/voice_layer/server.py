"""Localhost bridge for the buddy's browser client (spike).

The realtime model runs in the browser over WebRTC (the transport OpenAI
documents for clients that capture and play audio directly). Two things the
browser must NOT do itself:

- **Hold the API key.** It gets an ephemeral client secret instead, minted here.
- **Execute tools.** Tool calls are dispatched in this process, through the
  ``agentwire`` CLI allowlist in :mod:`~agentwire.voice_layer.tools`.

So this is a deliberately tiny stdlib HTTP server with exactly three routes. It
is **not** the portal and must never become part of it: it binds ``127.0.0.1``
only, defaults to a non-default port, and mints a fresh bearer token per run
that the page is served with. A tool-execution endpoint reachable from anywhere
else on the network is precisely the unguarded surface this design is trying not
to create.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import client, realtime, tools
from . import instructions as buddy_instructions

#: Not 8765 (portal SSL) and not 8100 (portal HTTP) — a spike must never
#: collide with the live install.
DEFAULT_PORT = 8788

_MAX_BODY = 64 * 1024


class BuddyBridge:
    """Request handling, independent of the HTTP plumbing (so it's testable)."""

    def __init__(self, buddy: str, token: str, *, model: str = "", voice: str = ""):
        self.buddy = buddy
        self.token = token
        self.model = model or realtime.DEFAULT_MODEL
        self.voice = voice or realtime.DEFAULT_VOICE

    def mint(self) -> dict:
        session = realtime.mint_session(
            instructions=buddy_instructions.build_instructions(),
            tools=tools.realtime_tool_defs(),
            model=self.model,
            voice=self.voice,
        )
        return {"success": True, **session}

    def tool_call(self, payload: dict) -> dict:
        name = payload.get("name")
        args = payload.get("arguments") or {}
        if not isinstance(name, str):
            return {"success": False, "error": "missing tool name"}
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                return {"success": False, "error": "malformed arguments JSON"}
        if not isinstance(args, dict):
            return {"success": False, "error": "arguments must be an object"}
        return tools.dispatch(name, args, self.buddy)


def _handler_factory(bridge: BuddyBridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agentwire-buddy-spike"

        def log_message(self, fmt, *args):  # quieter than the stdlib default
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

        def _authed(self) -> bool:
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            return secrets.compare_digest(supplied, bridge.token)

        def do_GET(self):  # noqa: N802  (stdlib naming)
            path = self.path.split("?", 1)[0]
            if path == "/":
                page = client.page(bridge.buddy, bridge.token).encode("utf-8")
                self._send(200, page, "text/html; charset=utf-8")
                return
            self._json(404, {"success": False, "error": "not found"})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if not self._authed():
                self._json(401, {"success": False, "error": "unauthorized"})
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), _MAX_BODY)
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"success": False, "error": "malformed request body"})
                return

            if path == "/mint":
                try:
                    self._json(200, bridge.mint())
                except realtime.RealtimeError as exc:
                    self._json(502, {"success": False, "error": str(exc)})
                return
            if path == "/tool":
                self._json(200, bridge.tool_call(payload))
                return
            self._json(404, {"success": False, "error": "not found"})

    return Handler


def serve(
    buddy: str,
    *,
    port: int = DEFAULT_PORT,
    model: str = "",
    voice: str = "",
) -> tuple[ThreadingHTTPServer, str]:
    """Start the bridge on ``127.0.0.1``. Returns the server and its URL."""
    token = secrets.token_urlsafe(24)
    bridge = BuddyBridge(buddy, token, model=model, voice=voice)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler_factory(bridge))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"
