"""Localhost bridge for the buddy's browser client (spike).

The realtime model runs in the browser over WebRTC (the transport OpenAI
documents for clients that capture and play audio directly). Two things the
browser must NOT do itself:

- **Hold the API key.** It gets an ephemeral client secret instead, minted here.
- **Execute tools.** Tool calls are dispatched in this process, through the
  ``agentwire`` CLI allowlist in :mod:`~agentwire.voice_layer.tools`.

So this is a deliberately tiny stdlib HTTP server with five routes. It is **not**
the portal and must never become part of it: it binds ``127.0.0.1`` only,
defaults to a non-default port, and mints a fresh bearer token per run that the
page is served with. A tool-execution endpoint reachable from anywhere else on
the network is precisely the unguarded surface this design is trying not to
create.

The loopback bind is **not** what keeps a remote page out, and reading it that
way is how the hole in #977 got here: the attacker never sends the packet, the
owner's browser does. A ``Host`` allowlist is the guard that actually
corresponds to the threat — see :func:`allowed_hosts`.

Two of the routes exist for the confirm gate's ordering:

- ``/utterance`` — the audio-commit boundary and the transcription model's
  output, both carrying the client's conversation-item sequence. See
  :mod:`~agentwire.voice_layer.transcript` for why the sequence and not a clock.
- ``/anchor`` — POSITIVE EVIDENCE that a proposal's announcement was spoken,
  which is when the owner heard what they would be approving. POSTed from the
  client's ``onSpoken`` for BOTH of its paths: a ``response.done`` whose
  transcript carried the text, and the ``speechSynthesis`` fallback, which
  produces no model turn at all. An earlier wording tied this to "the
  ``response.done`` of the turn in which the buddy spoke it"; #951 retired it
  precisely because the fallback path has no such turn.

``ThreadingHTTPServer`` is load-bearing, not incidental, and in two directions:
a ``/tool`` request evaluating a confirm blocks briefly on the ring's condition
waiting for the ``/utterance`` request carrying the transcript it needs — on a
single-threaded server that is a deadlock — and the ring is therefore shared
mutable state across request threads, which is why it holds a lock on every
access.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import client, confirm, identity, realtime, tools, transcript, write_tools
from . import instructions as buddy_instructions

#: Not 8765 (portal SSL) and not 8100 (portal HTTP) — a spike must never
#: collide with the live install.
DEFAULT_PORT = 8788

_MAX_BODY = 64 * 1024

#: The only names that may appear in a request's ``Host``. Loopback literals
#: plus the name a human types for them — see :func:`allowed_hosts` for why the
#: set is exactly this and no larger.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

#: How far ``/mint`` advances the logical clock before handing the page its
#: origin (#978). One per page load, so this is what makes the base an EPOCH
#: rather than a bump: two tabs minting against one bridge get non-overlapping
#: numeric ranges — reserved atomically, see
#: :meth:`~agentwire.voice_layer.transcript.TranscriptRing.reserve_epoch` — and
#: a still-live first tab would have to emit a million data-channel events
#: before it could count into the second's.
MINT_SEQ_GAP = 1_000_000

#: The largest sequence this bridge will accept or hand out.
#:
#: A gap does NOT cost nothing, and saying so was wrong in a way that only
#: shows up after ``seq_base`` existed. Before it, ``high_seq`` was state the
#: bridge kept for its own ordering — a Python int, compared, never sized. It
#: now flows BACK into the page's counter across a JSON boundary, and on that
#: side it is an IEEE-754 double: past ``2**53`` an increment silently stops
#: advancing, so every event shares one sequence, ``after(anchor)`` is never
#: strictly-after, and the buddy answers ``pending_transcript`` forever. Larger
#: still parses as ``Infinity``, whose anchors serialize as ``null`` —
#: ``not_announced`` forever. Both are silent, and both survive a reload,
#: because ``high_seq`` is bridge-lifetime: one malformed local POST wedges the
#: buddy for the rest of the run.
#:
#: ``2**45`` leaves 35 million mints of headroom and stays 256x clear of the
#: page's safe-integer limit, so the false-reject half costs nothing real: no
#: session reaches within many orders of magnitude of it. Loopback- and
#: token-gated, so this is robustness rather than a remote attack — but #986
#: hardened this same bridge against this same class.
MAX_SEQ = 2**45


def allowed_hosts(port: int) -> frozenset[str]:
    """Every ``Host`` value this bridge will answer on ``port``.

    Binding ``127.0.0.1`` does not make the bridge unreachable from the web,
    because the attacker is not sending the packet — the OWNER'S BROWSER is.
    A page on ``evil.com`` that rebinds its own name to ``127.0.0.1`` becomes
    same-origin with the bridge, fetches ``/`` (served with no auth), reads the
    ``TOKEN`` embedded in the page, and POSTs ``/tool`` with it. The one thing
    it cannot forge is ``Host``: the browser sets it from the address bar, so
    the rebound request says ``evil.com`` and the real client says a loopback
    name.

    **The false-reject half is the expensive one.** This is a screenless
    channel: a refused local client is not an error the owner reads, it is a
    buddy that stops working with no explanation. So the set is derived from
    how the client ACTUALLY connects, not from a guess:

    - ``client.py`` fetches ``/mint``, ``/tool``, ``/utterance`` and ``/anchor``
      as RELATIVE paths, so ``Host`` is always whatever is in the address bar
      — never a value the page chooses.
    - ``serve()`` returns ``http://127.0.0.1:<port>/`` and ``agentwire buddy
      serve`` prints exactly that, which makes ``127.0.0.1:<port>`` the common
      case.
    - ``localhost:<port>`` is what a human types instead, and ``[::1]:<port>``
      is what a browser shows if it reaches for IPv6 first. The bind is
      IPv4-only, so that last one normally fails at TCP rather than here — but
      if it ever connects it is still the loopback, and refusing it would be a
      false reject for no gain.
    - A browser omits the port when it is the scheme default, so port 80 also
      accepts the bare names. The bridge never defaults there, but ``--port
      80`` is expressible and would otherwise refuse the only ``Host`` a
      browser can send.

    Matching is exact against this set (case-folded — ``Host`` is a hostname).
    Anything else, including a name that RESOLVES to loopback, is foreign:
    resolution is precisely what rebinding controls.

    What this does not close, and is not claimed to: on a multi-user machine
    any other local user can ``curl`` ``/`` and take the token, because
    loopback is per-host, not per-user.
    """
    hosts = {f"{name}:{port}" for name in LOOPBACK_HOSTS}
    if port == 80:
        hosts |= set(LOOPBACK_HOSTS)
    return frozenset(hosts)


def host_allowed(header: str | None, port: int) -> bool:
    """Whether a ``Host`` header value may be served on ``port``.

    A missing header refuses. HTTP/1.0 permits omitting it, but nothing a
    browser does omits it — so this costs no real client, while accepting an
    absent ``Host`` would make the guard bypassable by anything hand-rolling a
    request.
    """
    return (header or "").strip().lower() in allowed_hosts(port)


class BuddyBridge:
    """Request handling, independent of the HTTP plumbing (so it's testable)."""

    def __init__(
        self,
        buddy: str,
        token: str,
        *,
        model: str = "",
        voice: str = "",
        runner=None,
    ):
        self.buddy = buddy
        self.token = token
        self.model = model or realtime.DEFAULT_MODEL
        # Explicit flag → the buddy's recorded voice → the default (#1017).
        # `register --voice` used to write a key nothing read.
        self.voice = identity.resolve_voice(buddy, voice)
        # One ring and one gate per bridge — per CONVERSATION, in effect. Not
        # module-level: a process-global store of pending writes would outlive
        # the conversation that proposed them.
        self.ring = transcript.TranscriptRing()
        self.spine = confirm.ConfirmSpine(
            self.ring, runner=runner or write_tools.dispatch_write
        )

    def utterance(self, payload: dict) -> dict:
        """Record a speech-start, an audio commit, or a completed transcript.

        Three shapes on one route, all carrying the client's conversation-item
        sequence:

        - ``{"item_id": …, "speech_started_seq": N}`` — the owner BEGAN
          speaking. The intent time, and the only one the gate orders on.
        - ``{"item_id": …, "commit_seq": N}`` — the audio buffer closed.
          Recorded for inspection and item binding; never gates. Ordering on
          the commit approves the barge-in case — see transcript.py.
        - ``{"item_id": …, "transcript": …, …}`` — the transcription model's
          text, carrying whichever sequences the client has for that item.
        """
        item_id = payload.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            return {"success": False, "error": "missing item_id"}
        item_id = item_id.strip()

        def _seq(key: str) -> "int | None":
            """The sequence under *key*, 0 if absent, ``None`` if out of range.

            Out-of-range is REFUSED rather than clamped: clamping would still
            raise ``high_seq`` toward the ceiling, and a silently-altered
            sequence is a silently-altered ordering. See :data:`MAX_SEQ`.
            """
            value = payload.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                return 0
            if value <= 0:
                return 0
            return value if value <= MAX_SEQ else None

        speech_seq, commit_seq = _seq("speech_started_seq"), _seq("commit_seq")
        if speech_seq is None or commit_seq is None:
            return {"success": False, "error": f"sequence exceeds {MAX_SEQ}"}
        text = payload.get("transcript")

        if text is None:
            if not (speech_seq or commit_seq):
                return {
                    "success": False,
                    "error": "need a positive speech_started_seq or commit_seq",
                }
            if speech_seq:
                self.ring.speech_started(item_id, speech_seq)
            if commit_seq:
                self.ring.commit(item_id, commit_seq)
            return {
                "success": True,
                "recorded": "speech_started" if speech_seq else "commit",
            }

        if not isinstance(text, str):
            return {"success": False, "error": "transcript must be a string"}
        if speech_seq:
            self.ring.speech_started(item_id, speech_seq)
        if commit_seq:
            self.ring.commit(item_id, commit_seq)
        entry = self.ring.transcribe(item_id, text)
        return {
            "success": True,
            "recorded": "transcript",
            # True when no speech_started was ever seen: the entry cannot be
            # placed in conversation order, so it will never approve.
            "estimated": entry.estimated,
            "speech_started_seq": entry.speech_started_seq,
        }

    def anchor(self, payload: dict) -> dict:
        """Record positive evidence that a proposal's announcement was SPOKEN.

        Until this lands the proposal is not confirmable — at propose time the
        owner has not yet heard what they would be approving, and barge-in is
        native on WebRTC.

        The client sends this from ``onSpoken``, for the model path AND the
        ``speechSynthesis`` fallback. An earlier docstring said "the
        ``response.done`` in which it was spoken", which is false for the
        fallback: it produces no model turn, so a fallback-announced proposal
        would never be anchored and a correct nonce would be refused to the TTL.
        """
        proposal_id = payload.get("proposal_id")
        seq = payload.get("seq")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            return {"success": False, "error": "missing proposal_id"}
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            return {"success": False, "error": "anchor needs a positive seq"}
        if seq > MAX_SEQ:
            return {"success": False, "error": f"sequence exceeds {MAX_SEQ}"}
        self.ring.note_seq(seq)
        anchored = self.spine.announce(proposal_id.strip(), seq)
        return {"success": True, "anchored": anchored, "seq": seq}

    def switch_voice(self, voice: str) -> dict:
        """Adopt *voice* for this bridge, and persist it for the next run.

        Called from :meth:`mint` when the page's picker sends one. Validated
        BEFORE anything is spent or written — an unknown voice is a refusal
        with the list in it, never a session that connects and sounds wrong.

        The persist is best-effort and guarded, on the same rule every optional
        extra in this package follows (``store_session_metadata`` raises by
        design, #885): the owner's live call must not fail because the record
        could not be updated. What they lose is stickiness across the next
        ``serve``, and they lose it loudly in the returned payload rather than
        silently.
        """
        voice = realtime.validate_voice(voice)
        if not voice or voice == self.voice:
            return {"changed": False, "voice": self.voice}
        self.voice = voice
        persisted, error = True, ""
        try:
            identity.set_voice(self.buddy, voice)
        except Exception as exc:  # noqa: BLE001  # never fatal to a live call
            persisted, error = False, str(exc)
        result = {"changed": True, "voice": voice, "voice_persisted": persisted}
        if error:
            result["voice_persist_error"] = error
        return result

    def mint(self, payload: "dict | None" = None) -> dict:
        """Mint an ephemeral client secret — and the page's CLOCK ORIGIN (#978).

        An optional ``voice`` in the body switches the buddy's voice for this
        session and every later one (#1017). It rides ``/mint`` rather than a
        route of its own because a voice change IS a new session: the API fixes
        the voice once the model has emitted audio (see
        :data:`~agentwire.voice_layer.realtime.VOICE_IS_SESSION_FIXED`), and
        the buddy greets on connect, so there is no live call whose voice could
        be updated in place. The page therefore tears down and re-mints — the
        one click is the product; the reconnect is the mechanism.

        The logical clock the confirm gate orders on is assigned by the client,
        because the client is the only place that sees data-channel event
        order. Its ORIGIN cannot be: ``seqCounter`` is a page variable and
        restarts at 0 on every reload, while the ring and the spine live for
        the whole bridge run. A reloaded page therefore anchored its next
        proposal BELOW last session's utterances, which are still in the ring,
        complete and unspent — so they reached the judge as non-matching
        (burning attempts on a question never asked) and, worse, an old "no,
        hang on" sat strictly-after the new match in the post-approval denial
        scan and retroactively denied every legitimate approval until 32 fresh
        utterances evicted it.

        ``/mint`` is the one event that happens exactly once per page load, so
        the origin is handed out here, a whole :data:`MINT_SEQ_GAP` above every
        sequence the bridge has ever seen, and RECORDED on the ring — a base
        the ring has not seen is a base the next mint would reuse. Reserved
        under ONE lock (``reserve_epoch``): read-then-write is two acquisitions
        and two concurrent mints on this threading server can be handed the
        same base, which is the very case the epoch exists to rule out.

        Reserved BEFORE the client secret, so an exhausted sequence space
        refuses without spending the owner's API key.

        Nothing is rejected and nothing is deleted, deliberately. A rejecting
        epoch guard pays its false-reject half by dropping an utterance from
        the owner's LIVE tab, and a dropped utterance in a screenless channel
        is a silent loop; ordering the epochs instead has no reject path at
        all. What it does not close: a forward that FAILS leaves ``high_seq``
        behind the client's counter, so the next base is that much less clear
        of the last epoch — bounded by the gap, not by anything smaller.
        """
        # Before the epoch and before the key: a refused voice must cost
        # nothing, not a burnt sequence epoch.
        try:
            switched = self.switch_voice((payload or {}).get("voice") or "")
        except realtime.RealtimeError as exc:
            return {"success": False, "error": str(exc)}
        seq_base = self.ring.reserve_epoch(MINT_SEQ_GAP, MAX_SEQ)
        if not seq_base:
            return {"success": False, "error": "sequence space exhausted"}
        session = realtime.mint_session(
            instructions=buddy_instructions.build_instructions(),
            tools=tools.realtime_tool_defs(),
            model=self.model,
            voice=self.voice,
        )
        return {
            "success": True,
            "seq_base": seq_base,
            "voice": self.voice,
            "voice_changed": switched["changed"],
            **{k: v for k, v in switched.items() if k.startswith("voice_")},
            **session,
        }

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
        return tools.dispatch(name, args, self.buddy, self.spine)


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

        def _host_ok(self) -> bool:
            """Checked FIRST on every route, GET included.

            ``/`` is served with no auth at all, so it is the request that
            hands the token over — a guard that ran only on the authenticated
            POSTs would be checking the door after the key was taken.

            The port comes from the LISTENING SOCKET rather than from an
            argument: ``serve()`` may be given port 0, and an allowlist built
            from the requested port would then match nothing at all.

            Two ``Host`` headers are refused outright rather than resolved to
            the first: which one a proxy or parser believes is the ambiguity,
            and no browser sends two.
            """
            values = self.headers.get_all("Host") or []
            if len(values) != 1:
                return False
            return host_allowed(values[0], self.server.server_address[1])

        def _authed(self) -> bool:
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            return secrets.compare_digest(supplied, bridge.token)

        def do_GET(self):  # noqa: N802  (stdlib naming)
            if not self._host_ok():
                self._json(403, {"success": False, "error": "forbidden host"})
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                # The voice comes off the BRIDGE, not off a constant: a switch
                # made in one tab is what a reload must come back showing.
                page = client.page(
                    bridge.buddy, bridge.token, voice=bridge.voice
                ).encode("utf-8")
                self._send(200, page, "text/html; charset=utf-8")
                return
            self._json(404, {"success": False, "error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                self._json(403, {"success": False, "error": "forbidden host"})
                return
            path = self.path.split("?", 1)[0]
            if not self._authed():
                self._json(401, {"success": False, "error": "unauthorized"})
                return
            try:
                # Clamped at BOTH ends. ``min`` alone let a negative through,
                # and ``read(-1)`` is read-to-EOF: the handler parked until
                # the client went away, with the request never having to send
                # a body at all. ``max`` alone would drop the 64K cap.
                # (Which way round the two nest does not matter; that both are
                # present does.)
                #
                # WHAT THIS DOES NOT CLOSE: the parked-thread class itself.
                # A request declaring ``Content-Length: 60000`` and sending 2
                # bytes still parks this thread in ``read(60000)`` until the
                # client disconnects — measured, 5 requests, 5 parked threads,
                # on this fixed code. Only the negative case is closed here.
                # Closing the rest needs a read timeout on the connection, not
                # a bound on the declared length.
                length = max(
                    0, min(int(self.headers.get("Content-Length") or 0), _MAX_BODY)
                )
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"success": False, "error": "malformed request body"})
                return

            if path == "/mint":
                try:
                    self._json(200, bridge.mint(payload))
                except realtime.RealtimeError as exc:
                    self._json(502, {"success": False, "error": str(exc)})
                return
            if path == "/tool":
                self._json(200, bridge.tool_call(payload))
                return
            if path == "/utterance":
                self._json(200, bridge.utterance(payload))
                return
            if path == "/anchor":
                self._json(200, bridge.anchor(payload))
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
