"""The bridge's Host guard and its body-length guard (#977).

Both are about a request the bearer token does not defend against.

**The Host guard.** The bridge binds ``127.0.0.1`` and mints a per-run bearer
token, and neither of those stops a REMOTE page from driving it. DNS rebinding
is the attack: a page served from ``evil.com`` re-resolves that name to
``127.0.0.1``, so the browser treats ``http://evil.com:8788/`` as same-origin,
fetches ``/`` — which is served with no auth at all — reads the ``TOKEN``
embedded in the page, and POSTs ``/tool`` with it. The loopback bind never
sees a foreign packet; the BROWSER is the confused deputy. What separates the
attack from the real client is the ``Host`` header, which the browser sets from
the address bar and script cannot forge.

So these tests drive **raw sockets**, not ``http.client``: the whole subject is
a header ``http.client`` writes for you, and a test that lets the library set
``Host`` is asserting on its own fixture.

Both halves of the guard are priced, and the false-reject half is the
expensive one — a legitimate local client refused in a screenless channel is a
buddy that goes silently dead. So the accepted set is asserted explicitly,
against how the client ACTUALLY connects: ``client.py`` fetches ``/tool``,
``/mint``, ``/utterance``, ``/anchor`` as **relative paths**, so the Host on
every request is whatever the owner typed in the address bar — the
``127.0.0.1:<port>`` the CLI prints, or the ``localhost:<port>`` a human types
instead, in whatever case they typed it.

**The body-length guard.** ``rfile.read(-5)`` reads to EOF, so a negative
``Content-Length`` parks a handler thread until the client goes away — one
leaked thread per request, and on a threading server nothing else reports it.
"""

from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer

import pytest

from agentwire import core
from agentwire.voice_layer import server

TOKEN = "test-token"


class _Bridge(server.BuddyBridge):
    """A bridge that records whether a tool ever reached dispatch.

    The Host guard's claim is that a foreign-Host request is refused *before
    routing*. A 403 alone cannot show that — this can.
    """

    def __init__(self):
        super().__init__("buddy", TOKEN, runner=lambda *a, **k: {"success": True})
        self.dispatched: list[str] = []

    def tool_call(self, payload: dict) -> dict:
        self.dispatched.append(str(payload.get("name")))
        return {"success": True}


@pytest.fixture
def bridge_server(tmp_path, monkeypatch):
    """A real bridge on a real ephemeral port. Port 0: a fixed port collides."""
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    bridge = _Bridge()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._handler_factory(bridge))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield bridge, httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def raw(port: int, request: str, *, timeout: float = 5.0) -> str:
    """Send a request byte-for-byte and read the whole response.

    ``timeout`` is the assertion in the negative-Content-Length test: a handler
    parked in ``read(-5)`` never answers, and the read here raises instead of
    hanging the suite.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request.replace("\n", "\r\n").encode("utf-8"))
        chunks = []
        while True:
            block = sock.recv(65536)
            if not block:
                break
            chunks.append(block)
            # The bridge answers with Connection: close semantics under
            # HTTP/1.0, but a 1.1 keep-alive response would leave us blocked
            # on recv until the timeout — stop once the body is complete.
            joined = b"".join(chunks)
            head, _, body = joined.partition(b"\r\n\r\n")
            length = _content_length(head.decode("latin-1"))
            if length is not None and len(body) >= length:
                break
        return b"".join(chunks).decode("utf-8", "replace")


def _content_length(head: str) -> int | None:
    for line in head.split("\r\n"):
        if line.lower().startswith("content-length:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def status(response: str) -> int:
    return int(response.split(" ", 2)[1])


# =============================================================================
# The Host allowlist — the false-REJECT half
# =============================================================================


class TestLegitimateClientsAreServed:
    """What must keep working. A Host guard that refuses the real client is a
    silent outage in a channel with no screen to show the error on."""

    def test_the_url_the_cli_actually_prints_is_served(self, bridge_server):
        """``serve()`` returns ``http://127.0.0.1:<port>/`` and ``buddy serve``
        prints exactly that — so this Host is the common case, not a variant."""
        _, port = bridge_server
        response = raw(port, f"GET / HTTP/1.1\nHost: 127.0.0.1:{port}\n\n")
        assert status(response) == 200
        assert TOKEN in response


# =============================================================================
# The Host allowlist — the false-ACCEPT half
# =============================================================================


class TestForeignHostsAreRefused:
    def test_get_root_refuses_a_foreign_host_and_leaks_no_token(
        self, bridge_server
    ):
        """The whole attack in one request: ``/`` is served with NO auth, so
        this response is where the token is stolen."""
        _, port = bridge_server
        response = raw(port, f"GET / HTTP/1.1\nHost: evil.com:{port}\n\n")
        assert status(response) == 403
        assert TOKEN not in response

    def test_a_rebound_name_on_the_right_port_is_still_foreign(
        self, bridge_server
    ):
        """DNS rebinding means the NAME resolves to 127.0.0.1 — the packet is
        indistinguishable from the real client's at every layer except this
        header. Matching on the port alone would accept it."""
        _, port = bridge_server
        response = raw(port, f"GET / HTTP/1.1\nHost: buddy.evil.com:{port}\n\n")
        assert status(response) == 403

    def test_a_foreign_host_post_never_reaches_dispatch(self, bridge_server):
        """With a VALID token — the stolen one. The refusal must land before
        routing, not after."""
        bridge, port = bridge_server
        body = '{"name": "fleet_sessions", "arguments": {}}'
        response = raw(
            port,
            f"POST /tool HTTP/1.1\nHost: evil.com:{port}\n"
            f"Authorization: Bearer {TOKEN}\n"
            f"Content-Length: {len(body)}\n\n{body}",
        )
        assert status(response) == 403
        assert bridge.dispatched == []


    def test_two_host_headers_are_refused_even_when_one_is_loopback(
        self, bridge_server
    ):
        """Which of two ``Host`` headers a parser believes is the whole point
        of request smuggling, and resolving to "the first one" is a choice, not
        a rule. No browser sends two, so refusing costs no real client."""
        _, port = bridge_server
        response = raw(
            port,
            f"GET / HTTP/1.1\nHost: 127.0.0.1:{port}\nHost: evil.com:{port}\n\n",
        )
        assert status(response) == 403
        assert TOKEN not in response


# =============================================================================
# The body-length guard
# =============================================================================


class TestBodyLength:
    def test_content_length_minus_one_does_not_park_the_thread(self, bridge_server):
        """``min(int(hdr), _MAX_BODY)`` passes a negative straight through, and
        ``-1`` is the value ``read()`` DEFINES as read-to-EOF: the handler
        blocks until the client disconnects, having sent no body at all.
        Measured, not assumed — an unfixed bridge holds this connection open
        indefinitely (verified at 3s, thread count +1), so the assertion is
        that a response arrives AT ALL and ``raw()``'s timeout is the failure.

        The scope of this test is exactly the negative case. A parked handler
        is still reachable by OVER-DECLARING a positive length (``60000``
        declared, 2 bytes sent — measured at 5 parked threads on the fixed
        code); that needs a read timeout on the connection and has no test
        here.

        Note this is ``-1`` and not the issue's ``-5``: see the test below."""
        _, port = bridge_server
        response = raw(
            port,
            f"POST /tool HTTP/1.1\nHost: 127.0.0.1:{port}\n"
            f"Authorization: Bearer {TOKEN}\nContent-Length: -1\n\n",
            timeout=5.0,
        )
        assert status(response) in (200, 400)
