"""Voice-loop preflight checks for ``agentwire doctor``.

Walks the live push-to-talk path end to end and reports each stage as
pass/fail with a single actionable fix line when red — so a broken first
session fails *loudly with a next step* instead of seeming dead.

There are two PTT paths and these checks cover what the CLI can honestly
inspect across both:

* **Browser PTT** (any device → portal → STT shim → tmux session): the mic
  is the browser's ``getUserMedia`` (granted in-browser, not host-checkable),
  but the portal, STT shim, and tmux last-leg all run on this host.
* **Host PTT** (macOS Hammerspoon ⌥Space → ``agentwire listen`` → host mic →
  custom STT shim → tmux session): the host mic, the Hammerspoon binding, and
  tmux are all locally inspectable.

Logic lives here as pure functions returning :class:`StageResult` so each
stage is unit-testable in isolation; ``cmd_doctor`` only renders them.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StageResult:
    """One voice-loop stage outcome.

    ``status`` is ``"ok"`` (green, working), ``"fail"`` (red, a real
    prerequisite for the configured path is down — counts as an issue), or
    ``"info"`` (neutral — not applicable / not host-checkable, never an issue).
    ``fixes`` are printed only when the stage is red.
    """

    name: str
    status: str  # "ok" | "fail" | "info"
    detail: str
    fixes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def _http_health(url: str, timeout: float = 2.0) -> tuple[bool, dict | None]:
    """GET ``{url}/health`` and parse JSON. Returns (reachable, payload).

    Accepts self-signed certs — the portal's HTTPS uses one, and rejecting it
    would falsely report a live portal as down.
    """
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/health")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            try:
                return True, json.loads(resp.read().decode())
            except (ValueError, UnicodeDecodeError):
                return True, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, None


# === Stage 1: mic / audio capture =========================================


def _find_ffmpeg() -> str | None:
    """Locate ffmpeg the same way the host PTT path does — delegating to the
    canonical resolver in ``agentwire.listen`` (config ``executables.ffmpeg`` →
    PATH → Homebrew fallbacks). Returns the path, or None if not found."""
    from .listen import _find_executable

    resolved = _find_executable("ffmpeg", ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"])
    # _find_executable returns the bare name as a last resort; treat that
    # (and any non-existent path) as "not found".
    if resolved and (Path(resolved).exists() or shutil.which(resolved)):
        return resolved
    return None


def _avfoundation_audio_devices(ffmpeg: str) -> list[str] | None:
    """List macOS AVFoundation audio input device names.

    ffmpeg prints the device table to stderr and exits non-zero (no output
    file) — that's expected. Returns the names, or None if the table couldn't
    be parsed at all. An empty list means the mic permission is denied or no
    input device is present.
    """
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    out = proc.stderr
    if "audio devices:" not in out:
        return None
    # Lines look like: [AVFoundation indev @ 0x..] [0] PD200X Podcast Microphone
    devices: list[str] = []
    in_audio = False
    for line in out.splitlines():
        if "audio devices:" in line:
            in_audio = True
            continue
        if in_audio:
            if "video devices:" in line:
                in_audio = False
                continue
            m = re.search(r"\]\s*\[(\d+)\]\s*(.+?)\s*$", line)
            if m:
                devices.append(m.group(2))
    return devices


def check_mic() -> StageResult:
    """Stage 1 — host audio-capture prerequisites.

    Checks ffmpeg (needed by the host PTT path and server-side wav handling)
    and, on macOS, that at least one AVFoundation audio input device is
    visible. Zero devices means the mic permission is revoked or no input
    exists. The browser PTT mic is granted in-browser per device and is not
    host-checkable — noted, never failed.
    """
    name = "Mic / audio capture"
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return StageResult(
            name, "fail", "ffmpeg not found (host capture + wav handling need it)",
            fixes=[
                "macOS: brew install ffmpeg",
                "Ubuntu: sudo apt install ffmpeg",
            ],
        )

    if platform.system() == "Darwin":
        devices = _avfoundation_audio_devices(ffmpeg)
        if devices is None:
            return StageResult(
                name, "info",
                f"ffmpeg present ({ffmpeg}); could not enumerate AVFoundation "
                "input devices — browser PTT mic is granted in-browser",
            )
        if not devices:
            return StageResult(
                name, "fail",
                "no audio input device visible (mic permission denied or none attached)",
                fixes=[
                    "Grant the mic: System Settings → Privacy & Security → "
                    "Microphone → enable your terminal app, then re-run",
                    "Or pick a device explicitly: set audio.input_device in "
                    "~/.agentwire/config.yaml",
                ],
            )
        return StageResult(
            name, "ok",
            f"{len(devices)} input device(s): {', '.join(devices)} "
            "(browser PTT mic is granted in-browser per device)",
        )

    # Non-macOS: ffmpeg is the checkable prerequisite; device permissions live
    # in the browser (browser PTT) or PulseAudio (host PTT).
    return StageResult(
        name, "ok",
        f"ffmpeg present ({ffmpeg}); browser PTT mic is granted in-browser per device",
    )


# === Stage 2: STT process ==================================================


def check_stt(config: Any) -> StageResult:
    """Stage 2 — the STT process behind transcription.

    Default tier = the portal-managed Moonshine ``:8101`` shim (#383); custom
    tier = a user/remote shim at ``stt.url``; cloud tier = an OpenAI-compatible
    API keyed from the environment.
    """
    name = "STT process"
    stt = getattr(config, "stt", None)
    backend = getattr(stt, "backend", "default") if stt is not None else "default"

    if backend == "cloud":
        import os
        cloud = getattr(stt, "cloud", None) or {}
        key_env = cloud.get("api_key_env", "OPENAI_API_KEY")
        if os.environ.get(key_env):
            return StageResult(name, "ok", f"cloud tier ({key_env} is set)")
        return StageResult(
            name, "fail", f"cloud tier but {key_env} is not set",
            fixes=[f"Add {key_env}=... to ~/.agentwire/.env (docs/wiki/security/secrets.md)"],
        )

    from .stt import DEFAULT_STT_URL, moonshine_importable

    if backend == "default" and not moonshine_importable():
        # py3.14+ or base install without the moonshine extra — push-to-talk
        # transcribes in the browser via SpeechRecognition. Works, no shim.
        return StageResult(
            name, "info",
            "browser SpeechRecognition fallback (Moonshine not installed for "
            "this Python — no host shim to run)",
        )

    url = getattr(stt, "url", None) or DEFAULT_STT_URL
    reachable, payload = _http_health(url)
    if reachable and (payload or {}).get("status") == "ok":
        # /health "model" may be a nested {backend, model, load_time} dict or a
        # bare string depending on the shim — surface the human-readable name.
        model = (payload or {}).get("model", "unknown")
        if isinstance(model, dict):
            model = model.get("model", "unknown")
        return StageResult(name, "ok", f"{backend} tier shim healthy at {url} (model {model})")

    if backend == "default":
        fixes = [
            "The portal auto-starts it; if it isn't running: agentwire stt start",
            "If it won't boot: uv pip install --python .venv/bin/python -e '.[stt]'",
        ]
    else:
        fixes = [
            "agentwire stt start",
            "If it won't boot: uv pip install --python .venv/bin/python -e '.[stt]'",
        ]
    return StageResult(
        name, "fail",
        f"{backend}-tier shim not responding at {url} — push-to-talk will fail "
        "(or fall back to browser speech) until it's back",
        fixes=fixes,
    )


# === Stage 3: tunnel / portal reachability =================================


def check_portal(config: Any, ctx: Any = None) -> StageResult:
    """Stage 3 — the portal the phone/browser talks to, and any reverse
    tunnel a remote device routes through to reach it."""
    name = "Tunnel / portal reachability"

    portal_cfg = getattr(config, "portal", None)
    portal_url = getattr(portal_cfg, "url", None) if portal_cfg else None
    if not portal_url:
        # Mirror _default_portal_url: https only when ssl cert+key exist.
        import os
        ssl = getattr(getattr(config, "server", None), "ssl", None)
        cert = getattr(ssl, "cert", None) if ssl else None
        key = getattr(ssl, "key", None) if ssl else None
        https = bool(
            cert and key
            and Path(os.path.expanduser(cert)).exists()
            and Path(os.path.expanduser(key)).exists()
        )
        portal_url = f"{'https' if https else 'http'}://localhost:8765"

    reachable, _ = _http_health(portal_url, timeout=3.0)
    if not reachable:
        return StageResult(
            name, "fail", f"portal not responding on {portal_url}",
            fixes=[
                "agentwire portal start        # or: agentwire portal start --dev",
                "Then re-run: agentwire doctor --voice",
            ],
        )

    # Portal is up. If remote machines route in over reverse tunnels, a down
    # tunnel breaks the loop for that device — surface it on this stage.
    down: list[str] = []
    if ctx is not None:
        try:
            from .tunnels import TunnelManager
            tm = TunnelManager()
            for spec in ctx.get_required_tunnels():
                status = tm.check_tunnel(spec)
                if status.status != "up":
                    down.append(
                        f"localhost:{spec.local_port} → "
                        f"{spec.remote_machine}:{spec.remote_port}"
                    )
        except Exception:
            pass

    if down:
        return StageResult(
            name, "fail",
            f"portal up on {portal_url}, but {len(down)} required tunnel(s) down: "
            + "; ".join(down),
            fixes=["agentwire tunnels up"],
        )
    return StageResult(name, "ok", f"portal responding on {portal_url}")


# === Stage 4: tmux wiring + PTT binding ====================================


def _tmux_server_running() -> bool:
    """True if a tmux server is up (it can list sessions, even if zero)."""
    try:
        proc = subprocess.run(
            ["tmux", "list-sessions"], capture_output=True, text=True, timeout=5
        )
        # rc 0 with sessions, or rc 1 "no server running" — both mean reachable
        # tmux binary; only a missing binary (OSError) is fatal here.
        return proc.returncode == 0 or "no server" not in proc.stderr.lower()
    except (subprocess.TimeoutExpired, OSError):
        return False


def _hammerspoon_ptt_bound() -> bool | None:
    """macOS host PTT binding: does ~/.hammerspoon/init.lua wire up
    ``agentwire listen``? Returns True/False, or None if the file is absent."""
    init = Path.home() / ".hammerspoon" / "init.lua"
    if not init.exists():
        return None
    try:
        text = init.read_text(errors="ignore")
    except OSError:
        return None
    return "listen" in text and "agentwire" in text


def check_tmux_ptt(config: Any) -> StageResult:
    """Stage 4 — tmux delivers the transcript's keystrokes to the session
    (the loop's last leg), plus the host ⌥Space PTT binding on macOS."""
    name = "tmux wiring + PTT binding"

    if not shutil.which("tmux"):
        return StageResult(
            name, "fail", "tmux not found — no way to deliver voice to a session",
            fixes=["macOS: brew install tmux", "Ubuntu: sudo apt install tmux"],
        )
    if not _tmux_server_running():
        return StageResult(
            name, "info",
            "tmux installed; no server running yet (start a session: agentwire new)",
        )

    # macOS host-PTT binding is informational — browser PTT works without it.
    if platform.system() == "Darwin":
        bound = _hammerspoon_ptt_bound()
        if bound is True:
            return StageResult(
                name, "ok",
                "tmux server reachable; Hammerspoon ⌥Space PTT binding present",
            )
        if bound is False:
            return StageResult(
                name, "info",
                "tmux server reachable; ~/.hammerspoon/init.lua exists but has no "
                "agentwire-listen binding — browser PTT still works",
            )
        return StageResult(
            name, "ok",
            "tmux server reachable (host ⌥Space PTT is optional: see "
            "examples/hammerspoon-ptt/; browser PTT needs no binding)",
        )

    return StageResult(name, "ok", "tmux server reachable")


def run_voice_loop_checks(config: Any, ctx: Any = None) -> list[StageResult]:
    """Run all four voice-loop stages in PTT order."""
    return [
        check_mic(),
        check_stt(config),
        check_portal(config, ctx),
        check_tmux_ptt(config),
    ]
