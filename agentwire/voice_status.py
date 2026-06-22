"""Active-tier voice status — the single source of truth for "can I speak /
hear right now, and through what path?".

Every status surface (``agentwire tts/stt status``, ``agentwire network``,
``agentwire doctor --voice``, and the MCP ``tts_status``/``stt_status`` tools)
resolves voice health through here so they all answer the same question the
same way.

The bug this fixes (#441): the old surfaces probed the **configured engine
server** regardless of the **active tier**, so they'd report "TTS not running"
while default-tier voice was working fine through the browser/OS, and they'd
probe the ``:8101`` STT shim even for the ``cloud`` tier (which has no shim).
The rule encoded here:

* resolve the **active tier** first,
* describe the path *that tier actually uses*,
* only probe a server when the tier actually has one
  (TTS ``custom``; STT ``custom`` and ``default`` when Moonshine is importable),
* flag an **orphaned** engine server (e.g. a TTS shim up on the custom-tier
  port while the tier is ``default`` — running but unused).
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VoiceStatus:
    """Resolved status of one voice subsystem (TTS or STT).

    ``ready`` answers the headline question — can this subsystem do its job
    right now over its active path. ``server_url`` is set only when the tier
    actually depends on a probed server. ``warnings`` carries non-fatal
    observations, most importantly an orphaned engine server (running but
    unused by the active tier).
    """

    subsystem: str          # "tts" | "stt"
    tier: str               # tts: default|custom ; stt: default|cloud|custom
    path: str               # human-readable description of the active path
    ready: bool             # can it work right now over that path?
    detail: str             # one-line explanation of the readiness state
    server_url: str | None = None   # set only when a server is part of the path
    server_probed: bool = False     # did we actually probe a server?
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "subsystem": self.subsystem,
            "tier": self.tier,
            "path": self.path,
            "ready": self.ready,
            "detail": self.detail,
            "server_url": self.server_url,
            "server_probed": self.server_probed,
            "warnings": self.warnings,
        }


def _describe_probe_error(exc: Exception) -> str:
    """Concise cause for a failed health probe (mirrors __main__'s helper)."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timed out (no response)"
    if isinstance(exc, json.JSONDecodeError):
        return "responded but returned invalid JSON"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "timed out (no response)"
        if isinstance(reason, ConnectionRefusedError):
            return "connection refused (not listening)"
        if isinstance(reason, socket.gaierror):
            return f"DNS resolution failed ({reason})"
        return f"unreachable ({reason})"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused (not listening)"
    return f"{type(exc).__name__}: {exc}"


def _probe(url: str, endpoint: str = "/health", timeout: float = 2.0) -> tuple[bool, str | None]:
    """GET ``{url}{endpoint}``. Returns (reachable, error_cause).

    Accepts self-signed certs (the portal/remote shims may use HTTPS).
    """
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}{endpoint}")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx):
            return True, None
    except Exception as e:  # noqa: BLE001 — any failure means "not reachable"
        return False, _describe_probe_error(e)


def _load_typed_config(config: Any | None):
    if config is not None:
        return config
    from .config import load_config
    return load_config()


# === TTS ===================================================================


def _tts_service_url() -> str | None:
    """The TTS shim location (NetworkContext ``tts`` service, default ``:8100``).

    This is the URL the ``custom`` tier actually POSTs to (matching ``say`` and
    ``_local_say_dispatch``). Under the ``default`` tier nothing routes here, so
    a server answering on it is an orphan.
    """
    try:
        from .network import NetworkContext
        return NetworkContext.from_config().get_service_url("tts", use_tunnel=True)
    except Exception:
        return None


def resolve_tts_status(config: Any | None = None, *, probe: bool = True) -> VoiceStatus:
    """Resolve the active TTS path.

    ``default`` tier routes audio to the browser portal when a client is
    connected, otherwise the OS voice (in-process Kokoro on local speakers once
    the model is cached, robotic OS ``say``/``espeak`` until then) — there is no
    server to probe, but a TTS shim left running on the custom-tier port is
    flagged as orphaned. ``custom`` tier POSTs to the configured shim, which is
    probed.
    """
    cfg = _load_typed_config(config)
    tier = getattr(getattr(cfg, "tts", None), "backend", "default")

    if tier == "custom":
        url = _tts_service_url() or getattr(cfg.tts, "url", None)
        if not url:
            return VoiceStatus(
                "tts", "custom", "custom shim (no url configured)", False,
                "tts.backend is 'custom' but no shim URL could be resolved",
            )
        ready, err = (_probe(url, endpoint="/voices") if probe else (True, None))
        return VoiceStatus(
            "tts", "custom", f"custom shim at {url}", ready,
            f"shim healthy at {url}" if ready else f"shim not responding at {url} — {err}",
            server_url=url, server_probed=probe,
        )

    # default tier — browser/OS, no shim in the path.
    warnings: list[str] = []
    try:
        from .tts.local import kokoro_importable
        importable = kokoro_importable()
    except Exception:
        importable = False
    cached = False
    if importable:
        try:
            from .tts.engines.kokoro import KokoroEngine
            cached = KokoroEngine.model_files_cached()
        except Exception:
            cached = False

    if cached:
        kokoro_state = "in-process Kokoro ready (local speakers when no browser)"
    elif importable:
        kokoro_state = "in-process Kokoro installed, model not downloaded (OS voice until cached)"
    else:
        kokoro_state = "in-process Kokoro unavailable (needs Python <3.14) — OS voice"

    # Orphan check: a TTS engine server answering on the custom-tier port is
    # unused under the default tier.
    if probe:
        orphan_url = _tts_service_url()
        if orphan_url:
            up, _ = _probe(orphan_url, endpoint="/voices")
            if up:
                warnings.append(
                    f"a TTS engine server is up at {orphan_url} but the active tier "
                    f"is 'default' — it is unused (default routes to browser/OS)."
                )

    return VoiceStatus(
        "tts", "default",
        "browser portal when a client is connected, otherwise OS voice",
        True,
        f"default tier: {kokoro_state}",
        warnings=warnings,
    )


# === STT ===================================================================


def resolve_stt_status(config: Any | None = None, *, probe: bool = True) -> VoiceStatus:
    """Resolve the active STT path.

    ``cloud`` uploads audio to an OpenAI-compatible API server-side — no shim,
    so readiness is "is the API key set". ``default`` transcribes via the
    portal-managed Moonshine ``:8101`` shim when Moonshine is importable, else
    falls back to in-browser SpeechRecognition (no host shim to probe).
    ``custom`` uploads to the configured shim, which is probed.
    """
    cfg = _load_typed_config(config)
    stt = getattr(cfg, "stt", None)
    tier = getattr(stt, "backend", "default") if stt is not None else "default"

    if tier == "cloud":
        cloud = getattr(stt, "cloud", None) or {}
        key_env = cloud.get("api_key_env", "OPENAI_API_KEY")
        ready = bool(os.environ.get(key_env))
        return VoiceStatus(
            "stt", "cloud", "cloud API (portal uploads audio server-side)", ready,
            f"{key_env} is set" if ready else f"{key_env} is NOT set in ~/.agentwire/.env",
        )

    from .stt import DEFAULT_STT_URL, moonshine_importable

    if tier == "default" and not moonshine_importable():
        # py3.14+ or base install — push-to-talk transcribes in the browser.
        return VoiceStatus(
            "stt", "default", "browser SpeechRecognition fallback", True,
            "Moonshine not installed for this Python — no host shim to run",
        )

    url = getattr(stt, "url", None) or DEFAULT_STT_URL
    ready, err = (_probe(url) if probe else (True, None))
    label = "Moonshine shim" if tier == "default" else "custom shim"
    return VoiceStatus(
        "stt", tier, f"{label} at {url}", ready,
        f"{tier}-tier shim healthy at {url}" if ready
        else f"{tier}-tier shim not responding at {url} — {err}",
        server_url=url, server_probed=probe,
    )
