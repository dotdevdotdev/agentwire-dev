"""Mints an OpenAI Realtime ephemeral session for the buddy (spike).

Shape verified against the current OpenAI docs (2026-08) and cross-checked
against a working implementation (DocumentScribe, whose request body was itself
derived from the ``openai-node`` SDK source rather than prose docs):

- ``POST https://api.openai.com/v1/realtime/client_secrets`` returns a
  short-lived client secret. ``value`` and ``expires_at`` are TOP-LEVEL;
  the session id is nested under ``session``.
- The browser then POSTs its SDP offer to
  ``https://api.openai.com/v1/realtime/calls`` with the client secret as the
  bearer token, and gets an SDP answer back.

The API key never leaves this process — the client gets an ephemeral secret
that expires in minutes. That is the whole reason minting is server-side.

``OPENAI_API_KEY`` comes from ``~/.agentwire/.env``, which ``__main__`` already
loads on every entry point. It is never read from ``config.yaml``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
CALLS_URL = "https://api.openai.com/v1/realtime/calls"

#: Current GA flagship realtime model (2026-07). NOT "gpt-voice-2" — that name
#: does not exist; see the docs findings in docs/wiki/voice-layer.md.
#:
#: When this id rotates, verify the replacement with ``GET /v1/models/<id>``.
#: Do NOT verify it by minting a client secret: the client_secrets endpoint
#: does not validate the model and returns 200 with a usable secret for an id
#: that does not exist (measured 2026-08-06 — "gpt-voice-2" minted fine, while
#: /v1/models 404s it). A mint that succeeds proves nothing about the model.
DEFAULT_MODEL = "gpt-realtime-2.1"

#: Every voice the Realtime API accepts, newest first (docs, fetched
#: 2026-08-11). ``cedar`` and ``marin`` are the two the docs single out as the
#: newer, more natural ones; the remaining eight predate them.
#:
#: Enumerated here rather than left as "any string" (#1017) because the failure
#: mode of a typo is the model-id footgun one level down: the mint endpoint is
#: not a validator. Whether it rejects an unknown VOICE is **not measured** —
#: what is known is that it returns 200 for a model that does not exist, so a
#: successful mint proves nothing, and in a screenless channel the owner's
#: evidence for "cedarr" being wrong is a buddy that never speaks. Validating
#: locally turns that into a sentence with the list in it, before the key is
#: spent.
#:
#: Ordered, not a set: this tuple is what ``--help``, the picker on the page,
#: and the error message all enumerate, and the owner reads them in this order.
VOICES = (
    "cedar", "marin", "alloy", "ash", "ballad",
    "coral", "echo", "sage", "shimmer", "verse",
)

#: One of the newer natural voices.
DEFAULT_VOICE = "cedar"

#: **The voice is fixed for the life of a realtime session.** The docs are
#: explicit: "Once the model has emitted audio in a session, the ``voice``
#: cannot be modified for that session." A ``session.update`` before the first
#: audio would take, but the buddy greets on connect (#963), so by the time
#: anyone picks a different voice the session has always emitted audio.
#:
#: That is why the picker on the page RECONNECTS instead of sending a
#: ``session.update``: the only honest mid-call voice change is a new session,
#: and the thing worth engineering is that it costs one click rather than a
#: kill, a re-serve and a manual reload. See :mod:`~agentwire.voice_layer.client`.
VOICE_IS_SESSION_FIXED = True

#: Transcribes the OWNER's audio, for the on-screen transcript and the log.
#: Independent of the conversational model above.
DEFAULT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

API_KEY_ENV = "OPENAI_API_KEY"


class RealtimeError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def voice_list() -> str:
    """The voices as one comma-separated line — one wording, three surfaces."""
    return ", ".join(VOICES)


def validate_voice(voice: str) -> str:
    """Return *voice*, or raise :class:`RealtimeError` naming every valid one.

    Empty means "unspecified" and is the caller's problem to default, not an
    error: every ``--voice`` on the CLI defaults to ``""``.

    Case-folded, because "Cedar" is a transcription of the same choice and
    refusing it would be a false reject with nothing behind it. Anything else
    refuses — including a name that merely looks plausible, which is the whole
    point: the list is short, closed, and printed in the refusal.
    """
    if not voice:
        return ""
    normalized = voice.strip().lower()
    if normalized not in VOICES:
        raise RealtimeError(
            f"unknown realtime voice {voice!r} — valid voices are: {voice_list()} "
            f"(default: {DEFAULT_VOICE})"
        )
    return normalized


def api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise RealtimeError(
            f"{API_KEY_ENV} is not set. Add it to ~/.agentwire/.env (chmod 600) — "
            "the one blessed spot for secrets."
        )
    return key


def build_session_request(
    *,
    instructions: str,
    tools: list[dict],
    model: str = DEFAULT_MODEL,
    voice: str = DEFAULT_VOICE,
) -> dict:
    """The ``client_secrets`` request body for a speech-to-speech session.

    ``turn_detection`` is ``semantic_vad``: it decides turn boundaries from what
    was said rather than from a silence timer, which matters here because the
    owner narrating a thought about the fleet pauses mid-sentence constantly.
    Barge-in comes for free — these models are full-duplex, and interrupting the
    buddy mid-sentence is a core interaction, not an error path.
    """
    return {
        "session": {
            "type": "realtime",
            "model": model,
            "instructions": instructions,
            "audio": {
                "input": {
                    # 24kHz PCM is what browser mic capture is documented to send.
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": DEFAULT_TRANSCRIPTION_MODEL},
                    "turn_detection": {"type": "semantic_vad"},
                },
                "output": {"voice": voice},
            },
            "tools": tools,
            "tool_choice": "auto",
        }
    }


def parse_session_response(payload: dict, requested_model: str) -> dict:
    """Narrow the mint response. ``value``/``expires_at`` are top-level."""
    secret = payload.get("value")
    session = payload.get("session") or {}
    session_id = session.get("id")
    if not isinstance(secret, str) or not secret or not isinstance(session_id, str):
        raise RealtimeError(
            "OpenAI client-secret response missing session.id or value", 502
        )
    return {
        "id": session_id,
        "client_secret": secret,
        "expires_at": payload.get("expires_at") or 0,
        "model": requested_model,
        "calls_url": CALLS_URL,
    }


def mint_session(
    *,
    instructions: str,
    tools: list[dict],
    model: str = DEFAULT_MODEL,
    voice: str = DEFAULT_VOICE,
    opener=None,
) -> dict:
    """Mint an ephemeral Realtime session. ``opener`` is injectable for tests.

    The voice is validated HERE as well as at every caller, deliberately: this
    is the last line before the owner's API key is spent, and a bad voice that
    gets past it costs a session that connects and then sounds like nothing.
    """
    voice = validate_voice(voice) or DEFAULT_VOICE
    body = json.dumps(
        build_session_request(
            instructions=instructions, tools=tools, model=model, voice=voice
        )
    ).encode("utf-8")
    request = urllib.request.Request(
        CLIENT_SECRETS_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RealtimeError(f"realtime mint failed ({exc.code}): {detail}", exc.code)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise RealtimeError(f"realtime mint failed: {exc}")
    return parse_session_response(payload, model)
