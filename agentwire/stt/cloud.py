"""STT backend that POSTs audio to an OpenAI-compatible transcription API.

The cloud tier: no local model, no shim daemon — the portal calls the
provider directly. The protocol (multipart ``file`` + ``model`` to
``{base_url}/audio/transcriptions`` with a Bearer key, JSON ``{"text"}``
response) is the de-facto industry standard: OpenAI, Groq, Mistral, and
self-hosted OpenAI-compatible servers (speaches, LocalAI, whisper.cpp
server) all speak it. Providers with their own protocol belong behind a
``custom`` shim instead.

The API key is read from the environment at backend construction (the
env var NAME comes from config — ``stt.cloud.api_key_env``). It is sent
only in the server-side Authorization header and never to the browser.
"""

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from .base import STTBackend

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


class CloudSTTBackend(STTBackend):
    """Transcribe via any OpenAI-compatible transcription endpoint."""

    @property
    def name(self) -> str:
        return "cloud"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: int = 30,
        language: str = "",
    ):
        if not api_key:
            raise ValueError("CloudSTTBackend requires an API key")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.language = language

    async def transcribe(self, audio_path: Path) -> str:
        """POST the audio to {base_url}/audio/transcriptions, return the text."""
        with open(audio_path, "rb") as f:
            audio_data = f.read()

        boundary = "----AgentWireBoundary"
        fields = [("model", self.model), ("response_format", "json")]
        if self.language:
            fields.append(("language", self.language))

        parts = [
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}"
            ).encode()
            for name, value in fields
        ]
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode() + audio_data
        )
        body = b"\r\n".join(parts) + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode())
                return payload.get("text", "").strip()
        except urllib.error.HTTPError as e:
            # Provider error bodies are useful (bad model, quota, ...) but may
            # echo request details — log status only at error level.
            detail = ""
            try:
                detail = e.read().decode()[:500]
            except Exception:
                pass
            logger.error(f"Cloud STT request failed: HTTP {e.code} {detail}")
            raise RuntimeError(f"Cloud STT error: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.error(f"Cloud STT request failed: {e}")
            raise RuntimeError(f"Cloud STT error: {e}") from e
