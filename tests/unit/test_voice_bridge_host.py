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

    def test_localhost_is_served(self, bridge_server):
        """Nobody types four octets. ``localhost`` resolves to the same bind
        and the browser sends it verbatim."""
        _, port = bridge_server
        response = raw(port, f"GET / HTTP/1.1\nHost: localhost:{port}\n\n")
        assert status(response) == 200
        assert TOKEN in response

    def test_the_host_header_is_matched_case_insensitively(self, bridge_server):
        """Host is a hostname: case is not significant, and a browser passes
        through whatever case was typed. Case-sensitive matching would refuse
        a legitimate client for the way they capitalised it."""
        _, port = bridge_server
        response = raw(port, f"GET / HTTP/1.1\nHost: LocalHost:{port}\n\n")
        assert status(response) == 200

    def test_ipv6_loopback_literal_is_served(self, bridge_server):
        """A browser resolving ``localhost`` may reach for ``::1`` first and
        will show the literal in the bar if it does. The bind is IPv4-only so
        this normally fails at TCP rather than here — but if it ever connects,
        it is the loopback and refusing it is a false reject."""
        _, port = bridge_server
        response = raw(port, f"GET / HTTP/1.1\nHost: [::1]:{port}\n\n")
        assert status(response) == 200

    def test_an_authorized_post_on_an_allowed_host_still_dispatches(
        self, bridge_server
    ):
        """The must-fail control for every refusal test below: if POST were
        broken outright, they would all pass for the wrong reason."""
        bridge, port = bridge_server
        body = '{"name": "fleet_sessions", "arguments": {}}'
        response = raw(
            port,
            f"POST /tool HTTP/1.1\nHost: 127.0.0.1:{port}\n"
            f"Authorization: Bearer {TOKEN}\n"
            f"Content-Length: {len(body)}\n\n{body}",
        )
        assert status(response) == 200
        assert bridge.dispatched == ["fleet_sessions"]


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

    @pytest.mark.parametrize("route", ["/mint", "/utterance", "/anchor"])
    def test_every_post_route_is_guarded(self, bridge_server, route):
        """Not just ``/tool``: ``/mint`` spends the owner's API key, and the
        two ordering routes feed the confirm gate's predicate."""
        _, port = bridge_server
        response = raw(
            port,
            f"POST {route} HTTP/1.1\nHost: evil.com:{port}\n"
            f"Authorization: Bearer {TOKEN}\nContent-Length: 2\n\n{{}}",
        )
        assert status(response) == 403

    def test_a_right_name_on_a_wrong_port_is_refused(self, bridge_server):
        """Another bridge — or another local service the browser has been
        rebound at — is not this one."""
        _, port = bridge_server
        response = raw(port, f"GET / HTTP/1.1\nHost: 127.0.0.1:{port + 1}\n\n")
        assert status(response) == 403

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

    def test_a_missing_host_is_refused(self, bridge_server):
        """HTTP/1.0 permits omitting it. Nothing the browser does omits it, so
        refusing costs no real client — and accepting an absent Host would make
        the guard trivially bypassable by anything that is not a browser."""
        _, port = bridge_server
        response = raw(port, "GET / HTTP/1.0\n\n")
        assert status(response) == 403
        assert TOKEN not in response


class TestHostPredicate:
    """The predicate on its own — the wire tests above cover one port each."""

    def test_the_accepted_set_is_exactly_the_loopback_names(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            assert server.host_allowed(f"{host}:8788", 8788)

    @pytest.mark.parametrize(
        "header",
        ["", "evil.com:8788", "127.0.0.1:8789", "127.0.0.1", "127.0.0.1:", "::1:8788"],
    )
    def test_everything_else_is_refused(self, header):
        assert not server.host_allowed(header, 8788)

    def test_a_bare_host_is_accepted_only_on_port_80(self):
        """Browsers omit the default port. The bridge never defaults to 80, but
        ``--port 80`` is expressible, and a guard that refuses the only Host a
        browser can send there is a guaranteed false reject."""
        assert server.host_allowed("localhost", 80)
        assert not server.host_allowed("localhost", 8788)


# =============================================================================
# The body-length guard
# =============================================================================


class TestBodyLength:
    def test_content_length_minus_one_does_not_park_the_thread(self, bridge_server):
        """``min(int(hdr), _MAX_BODY)`` passes a negative straight through, and
        ``-1`` is the value ``read()`` DEFINES as read-to-EOF: the handler
        blocks until the client disconnects, and on a threading server that is
        one leaked thread per request with nothing reporting it. Measured, not
        assumed — an unfixed bridge holds this connection open indefinitely
        (verified at 3s, thread count +1), so the assertion is that a response
        arrives AT ALL and ``raw()``'s timeout is the failure.

        Note this is ``-1`` and not the issue's ``-5``: see the test below."""
        _, port = bridge_server
        response = raw(
            port,
            f"POST /tool HTTP/1.1\nHost: 127.0.0.1:{port}\n"
            f"Authorization: Bearer {TOKEN}\nContent-Length: -1\n\n",
            timeout=5.0,
        )
        assert status(response) in (200, 400)

    def test_other_negative_lengths_answer_too(self, bridge_server):
        """A control that is GREEN BEFORE THE FIX, deliberately kept and
        labelled. ``read(-5)`` raises ``ValueError`` ("read length must be
        non-negative or -1"), which the existing ``except`` turns into a 400 —
        so the issue's own repro value never hung, and a suite that pinned the
        bug on ``-5`` alone would have shipped a green light over a live
        thread leak. After ``max(0, …)`` both values take the same path."""
        _, port = bridge_server
        response = raw(
            port,
            f"POST /tool HTTP/1.1\nHost: 127.0.0.1:{port}\n"
            f"Authorization: Bearer {TOKEN}\nContent-Length: -5\n\n",
            timeout=5.0,
        )
        assert status(response) in (200, 400)

    def test_an_oversized_body_is_still_truncated(self, bridge_server):
        """The existing 64K cap must survive the fix. Clamping at both ends is
        two guards, and dropping either one is a silent regression: this is the
        one that catches ``max(0, int(...))`` written without the ``min``."""
        _, port = bridge_server
        body = '{"name": "x", "pad": "' + "a" * (server._MAX_BODY * 2) + '"}'
        response = raw(
            port,
            f"POST /tool HTTP/1.1\nHost: 127.0.0.1:{port}\n"
            f"Authorization: Bearer {TOKEN}\n"
            f"Content-Length: {len(body)}\n\n{body}",
        )
        # Truncated at the cap, so the JSON no longer parses.
        assert status(response) == 400
