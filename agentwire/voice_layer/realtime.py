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
DEFAULT_MODEL = "gpt-realtime-2.1"

#: One of the newer natural voices.
DEFAULT_VOICE = "cedar"

#: Transcribes the OWNER's audio, for the on-screen transcript and the log.
#: Independent of the conversational model above.
DEFAULT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

API_KEY_ENV = "OPENAI_API_KEY"


class RealtimeError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


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
    """Mint an ephemeral Realtime session. ``opener`` is injectable for tests."""
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
