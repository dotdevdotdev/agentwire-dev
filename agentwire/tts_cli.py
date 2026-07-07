"""CLI for the audio service processes — ``agentwire tts|stt|kokoro``.

These command groups share the same local/remote service-spawn shape
(start a uvicorn server in a tmux session, or over SSH on a remote machine),
so they live in one module along with their venv-selection and restart
helpers. Shared, stateless helpers live in ``core``.

``cmd_stt_start`` keeps its name: a Wave-2 ``cmd_up`` imports it via
``from . import tts_cli``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .core import (
    _output_json,
    get_kokoro_session_name,
    get_source_dir,
    get_stt_session_name,
    get_tts_session_name,
    load_config,
    tmux_session_exists,
)

# === TTS Commands ===


def _get_venv_for_backend(backend: str) -> str:
    """Get the venv family required for a backend."""
    if backend.startswith("zonos"):
        return "zonos"
    if backend == "kokoro":
        return "kokoro"
    return "chatterbox"


def _get_tts_engine(args, tts_config: dict) -> str:
    """Resolve the TTS engine to run: --backend flag, then tts.options.backend.

    The top-level tts.backend is the tier (default|custom), never an engine.
    """
    return (
        getattr(args, "backend", None)
        or (tts_config.get("options") or {}).get("backend")
        or "chatterbox"
    )


def _start_tts_local(args, venv_override: str | None = None, attach: bool = True) -> int:
    """Start TTS server locally in tmux.

    Args:
        args: Parsed CLI arguments
        venv_override: Force specific venv (used for restart after venv_mismatch)
        attach: When False (used by `agentwire up`), start detached and return.
    """
    session_name = get_tts_session_name()

    if tmux_session_exists(session_name):
        print(f"TTS server already running in tmux session '{session_name}'")
        if attach:
            print("Attaching... (Ctrl+B D to detach)")
            subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    # Get TTS config
    config = load_config()
    tts_config = config.get("tts", {})
    port = args.port or tts_config.get("port", 8100)
    host = args.host or tts_config.get("host", "0.0.0.0")
    backend = _get_tts_engine(args, tts_config)

    # Determine venv family
    venv = venv_override or _get_venv_for_backend(backend)

    # Find the source directory and appropriate venv
    source_dir = get_source_dir()
    if not source_dir:
        print("Error: Cannot find agentwire source directory.", file=sys.stderr)
        return 1

    # Map venv family to venv directory name
    venv_name = f".venv-{venv}" if venv != "default" else ".venv"
    venv_path = source_dir / venv_name / "bin" / "activate"

    if not venv_path.exists():
        print(f"Error: Venv not found: {venv_path}", file=sys.stderr)
        print(f"Create it with: cd {source_dir} && uv venv {venv_name}", file=sys.stderr)
        return 1

    # Build command using venv python directly (avoids broken activate scripts and conda interference)
    venv_python = source_dir / venv_name / "bin" / "python"
    tts_cmd = (
        f"cd {source_dir} && "
        f"{venv_python} -m agentwire tts serve --host {host} --port {port} --backend {backend} --venv {venv}"
    )

    print(f"Starting TTS server on {host}:{port} (backend: {backend}, venv: {venv})...")
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name,
    ])
    subprocess.run([
        "tmux", "send-keys", "-t", session_name, tts_cmd, "Enter",
    ])

    if attach:
        print("TTS server started. Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
    else:
        print("TTS server started.")
    return 0


def _start_tts_remote(ssh_target: str, machine_id: str, args) -> int:
    """Start TTS server on remote machine via SSH."""
    session_name = get_tts_session_name()

    # Check if TTS already running remotely
    check_cmd = f"tmux has-session -t ={session_name} 2>/dev/null && echo running || echo stopped"
    result = subprocess.run(
        ["ssh", ssh_target, check_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Cannot reach TTS machine. Check: ssh {ssh_target} echo ok", file=sys.stderr)
        return 1

    if "running" in result.stdout:
        print(f"TTS server already running on {machine_id} in tmux session '{session_name}'")
        return 0

    # Get port config
    config = load_config()
    tts_config = config.get("tts", {})
    port = args.port or tts_config.get("port", 8100)
    host = args.host or tts_config.get("host", "0.0.0.0")
    backend = _get_tts_engine(args, tts_config)

    # Build backend flag
    backend_flag = f" --backend {backend}" if backend != "chatterbox" else ""

    # Build remote command - on remote machine, use agentwire tts serve
    server_cmd = f"agentwire tts serve --host {host} --port {port}{backend_flag}"

    # Start remotely in tmux
    remote_cmd = f"tmux new-session -d -s {session_name} && tmux send-keys -t {session_name} {shlex.quote(server_cmd)} Enter"
    print(f"Starting TTS server on {machine_id}...")

    result = subprocess.run(
        ["ssh", ssh_target, remote_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Failed to start TTS on {machine_id}: {result.stderr}", file=sys.stderr)
        return 1

    print(f"TTS server started on {machine_id}.")
    return 0


def _stop_tts_remote(ssh_target: str, machine_id: str) -> int:
    """Stop TTS server on remote machine via SSH."""
    session_name = get_tts_session_name()

    # Check if running
    check_cmd = f"tmux has-session -t ={session_name} 2>/dev/null && echo running || echo stopped"
    result = subprocess.run(
        ["ssh", ssh_target, check_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Cannot reach TTS machine. Check: ssh {ssh_target} echo ok", file=sys.stderr)
        return 1

    if "stopped" in result.stdout:
        print(f"TTS server is not running on {machine_id}.")
        return 1

    # Kill session
    kill_cmd = f"tmux kill-session -t {session_name}"
    result = subprocess.run(
        ["ssh", ssh_target, kill_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Failed to stop TTS on {machine_id}: {result.stderr}", file=sys.stderr)
        return 1

    print(f"TTS server stopped on {machine_id}.")
    return 0


def _describe_probe_error(exc: Exception) -> str:
    """Turn a health-probe exception into a concise, human-readable cause.

    Distinguishes the cases that actually matter when a voice service won't
    come up: connection refused, timeout, bad JSON, DNS failure, HTTP error.
    """
    import socket

    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return "timed out (no response)"
    if isinstance(exc, json.JSONDecodeError):
        return "responded but returned invalid JSON"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.timeout) or isinstance(reason, TimeoutError):
            return "timed out (no response)"
        if isinstance(reason, ConnectionRefusedError):
            return "connection refused (not listening yet)"
        if isinstance(reason, socket.gaierror):
            return f"DNS resolution failed ({reason})"
        return f"unreachable ({reason})"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused (not listening yet)"
    return f"{type(exc).__name__}: {exc}"


def cmd_tts_start(args) -> int:
    """Start the Chatterbox TTS server in tmux."""
    from .network import NetworkContext

    ctx = NetworkContext.from_config()

    if ctx.is_local("tts"):
        return _start_tts_local(args)

    # TTS runs on another machine
    ssh_target = ctx.get_ssh_target("tts")
    machine_id = ctx.get_machine_for_service("tts")

    if not ssh_target or not machine_id:
        print("TTS configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    print(f"TTS runs on {machine_id}, starting remotely...")
    return _start_tts_remote(ssh_target, machine_id, args)


def cmd_tts_serve(args) -> int:
    """Run the TTS server directly (foreground)."""
    import uvicorn

    config = load_config()
    tts_config = config.get("tts", {})
    port = args.port or tts_config.get("port", 8100)
    host = args.host or tts_config.get("host", "0.0.0.0")
    backend = _get_tts_engine(args, tts_config)

    # Determine venv family (explicit or auto-detect from backend)
    venv = getattr(args, "venv", None)
    if not venv:
        venv = _get_venv_for_backend(backend)

    # Set env vars for the TTS server module
    os.environ["DEFAULT_BACKEND"] = backend
    os.environ["CURRENT_VENV"] = venv

    print(f"Starting TTS server on {host}:{port} (backend: {backend}, venv: {venv})...")
    uvicorn.run(
        "agentwire.tts_server:app",
        host=host,
        port=port,
        log_level="info",
    )
    return 0


def cmd_tts_stop(args) -> int:
    """Stop the TTS server."""
    from .network import NetworkContext

    ctx = NetworkContext.from_config()
    session_name = get_tts_session_name()

    if ctx.is_local("tts"):
        if not tmux_session_exists(session_name):
            print("TTS server is not running.")
            return 1

        subprocess.run(["tmux", "kill-session", "-t", session_name])
        print("TTS server stopped.")
        return 0

    # TTS runs on another machine
    ssh_target = ctx.get_ssh_target("tts")
    machine_id = ctx.get_machine_for_service("tts")

    if not ssh_target or not machine_id:
        print("TTS configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    print(f"TTS runs on {machine_id}, stopping remotely...")
    return _stop_tts_remote(ssh_target, machine_id)


def cmd_tts_restart(args) -> int:
    """Restart TTS server with optional venv override.

    Used by CLI when venv_mismatch occurs during TTS request.
    Supports both local and remote TTS servers.
    """
    import time

    from .network import NetworkContext

    ctx = NetworkContext.from_config()
    session_name = get_tts_session_name()

    # Get overrides from args
    venv_override = getattr(args, "venv", None)
    backend = getattr(args, "backend", None)

    if ctx.is_local("tts"):
        # Stop if running
        if tmux_session_exists(session_name):
            print("Stopping TTS server...")
            subprocess.run(["tmux", "kill-session", "-t", session_name])
            time.sleep(1)

        # Start with new venv
        return _start_tts_local(args, venv_override=venv_override)

    # Remote TTS
    ssh_target = ctx.get_ssh_target("tts")
    machine_id = ctx.get_machine_for_service("tts")

    if not ssh_target or not machine_id:
        print("TTS configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    # Determine backend and venv
    if not backend:
        backend = _get_tts_engine(args, load_config().get("tts", {}))

    venv = venv_override or _get_venv_for_backend(backend)

    print(f"Restarting TTS on {machine_id} with backend '{backend}'...")
    success = _restart_tts_remote_for_venv(ssh_target, machine_id, venv, backend)
    return 0 if success else 1


def cmd_tts_warm(args) -> int:
    """Download the default-tier Kokoro model files and verify the engine loads.

    Blocking pre-download path for headless/CLI-only setups; the portal does
    the same download in the background on first start.
    """
    from .tts.local import kokoro_importable

    if not kokoro_importable():
        print("kokoro-onnx is not installed (requires Python >=3.10,<3.14).", file=sys.stderr)
        return 1

    from .tts.engines.kokoro import KokoroEngine

    if KokoroEngine.model_files_cached():
        print("Kokoro model files already cached (~/.cache/kokoro_onnx/)")
    else:
        last_pct: dict = {}

        def progress(filename: str, downloaded: int, total: int) -> None:
            pct = int(downloaded * 100 / total) if total else 0
            if pct >= last_pct.get(filename, -10) + 10:
                last_pct[filename] = pct
                print(f"  {filename}: {pct}%")

        KokoroEngine.download_models(progress)

    print("Loading engine (verification)...")
    engine = KokoroEngine()
    engine.unload()
    print("Kokoro voice ready.")
    return 0


def cmd_tts_status(args) -> int:
    """Report whether speech can happen right now and via what path.

    Resolves the ACTIVE tier (default → browser/OS; custom → shim) and only
    probes a server when the tier actually has one. Flags an orphaned engine
    server — e.g. a shim still up on the custom-tier port while the tier is
    'default' (running but unused) (#441).
    """
    from .config import load_config as load_config_typed
    from .voice_status import resolve_tts_status

    json_mode = getattr(args, 'json', False)
    session_name = get_tts_session_name()
    st = resolve_tts_status(load_config_typed())

    if json_mode:
        payload = {"success": True, **st.to_json()}
        if st.server_url:
            payload["session"] = session_name if tmux_session_exists(session_name) else None
        _output_json(payload)
        return 0 if st.ready else 1

    print(f"TTS: {st.tier} tier — {st.path}")
    print(f"  {'[ok]' if st.ready else '[!!]'} {st.detail}")
    if st.server_url and tmux_session_exists(session_name):
        print(f"  Attach: tmux attach -t {session_name}")
    if not st.ready and st.tier == "custom":
        print("  Start: agentwire tts start")
    if st.tier == "default":
        print("  Warm the in-process model now: agentwire tts warm "
              "(else the portal downloads it in the background, OS voice until ready)")
    for w in st.warnings:
        print(f"  [..] {w}")
    return 0 if st.ready else 1


def _resolve_shim_python() -> tuple[str | None, str | None, str | None]:
    """(python, cwd, error) for running a default-tier shim server.

    Prefers a source checkout's .venv (the dev workflow); otherwise the
    installed package's own interpreter, provided the shim's web wrapper
    (fastapi/uvicorn) is importable there. On a plain pip/uv-tool install
    without those deps, returns an actionable error instead of assuming a
    dev checkout exists (#634).
    """
    from .core import find_source_checkout

    source_dir = find_source_checkout()
    if source_dir:
        venv_python = source_dir / ".venv" / "bin" / "python"
        if venv_python.exists():
            return str(venv_python), str(source_dir), None
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return None, None, (
            "the shim's web wrapper (fastapi/uvicorn) is not installed in this "
            "environment.\n"
            "Install it with:  uv tool install 'agentwire-dev[stt]' --force\n"
            "  (pip: pip install 'agentwire-dev[stt]')\n"
            "Or run from a source checkout: git clone "
            "https://github.com/dotdevdotdev/agentwire-dev && cd agentwire-dev && uv sync"
        )
    return sys.executable, None, None


# === Managed shim liveness (health-aware idempotency, #734) ===
#
# A managed voice shim (Kokoro TTS :8102, Moonshine STT :8101) runs uvicorn in
# a tmux session and warms its model in the background. The old start path was
# idempotent on *session existence* alone, so a dead/wedged process inside a
# live session was treated as healthy forever — say/transcribe then silently
# fell back to browser voice. These helpers make start/ensure health-aware:
# reuse a session only when it is actually serving (or too young to have bound
# its port yet); otherwise reap it and relaunch.


def _probe_shim_health(port: int, timeout: float = 2.0) -> tuple[bool, str | None]:
    """Probe a managed shim's ``/health`` on localhost.

    Returns ``(responded, status)``: ``responded`` is True when the HTTP server
    answered at all (even mid-warmup); ``status`` is the reported state string
    (``ok``/``loading``/``downloading``/``absent``/``failed``/…) or None when
    nothing answered (process dead, wedged, or the port not yet bound)."""
    try:
        req = urllib.request.Request(f"http://localhost:{port}/health")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return True, data.get("status")
    except Exception:
        return False, None


def _tmux_session_age(session_name: str) -> float | None:
    """Seconds since the tmux session was created, or None if unknowable."""
    import time

    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session_name, "#{session_created}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return time.time() - int(result.stdout.strip())
    except (ValueError, TypeError):
        return None


def _shim_session_state(
    session_name: str, port: int, *, warmup_grace: float = 25.0
) -> tuple[bool, str | None]:
    """``(live, status)`` for an EXISTING managed-shim tmux session.

    The single liveness predicate shared by ``start`` (reuse-vs-reap) and
    ``doctor`` (flag-a-dead-shim). A session is *live* when its ``/health``
    answers a non-``failed`` status, OR it is younger than ``warmup_grace`` (the
    port may not be bound yet). A process that never answers past the grace, or
    reports a terminal ``failed``, is NOT live. ``status`` is the ``/health``
    state, ``"starting"`` for a still-booting young session, or None for a dead
    one — carried through for human-readable diagnostics."""
    responded, status = _probe_shim_health(port)
    if responded:
        return (status != "failed", status)
    age = _tmux_session_age(session_name)
    if age is not None and age < warmup_grace:
        return (True, "starting")
    return (False, status)


def _reap_shim_session(session_name: str) -> None:
    """Kill a dead shim tmux session so a fresh one can bind the port."""
    import time

    subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
    time.sleep(0.5)


# === STT Commands ===

def cmd_stt_start(args) -> int:
    """Start the STT server in tmux."""
    session_name = get_stt_session_name()
    port = args.port or 8101
    host = args.host or "0.0.0.0"

    if tmux_session_exists(session_name):
        live, status = _shim_session_state(session_name, port)
        if live:
            print(f"STT server already running in tmux session '{session_name}'")
            print(f"  Attach: tmux attach -t {session_name}")
            return 0
        # Session exists but :{port}/health isn't serving (dead/wedged) —
        # self-heal by reaping and relaunching instead of masking a dead
        # engine behind mere session existence (#734).
        print(
            f"STT server session '{session_name}' is present but not serving "
            f"(state: {status or 'no response'}) — relaunching."
        )
        _reap_shim_session(session_name)

    config = load_config()
    stt_config = config.get("stt", {})
    model = args.model or stt_config.get("model", "base")
    backend = getattr(args, 'backend', None) or stt_config.get("engine", "auto")
    moonshine_model = stt_config.get("moonshine_model", "moonshine/base")

    # Resolve an interpreter that can run the shim: dev-checkout venv when one
    # exists, otherwise the installed package's interpreter.
    python_path, cwd, error = _resolve_shim_python()
    if error:
        print(f"Error: cannot start the STT shim — {error}", file=sys.stderr)
        return 1
    run_dir = cwd or str(Path.home())

    cmd = f"cd {run_dir} && WHISPER_MODEL={model} WHISPER_DEVICE=cpu STT_PORT={port} STT_HOST={host} STT_BACKEND={backend} MOONSHINE_MODEL={moonshine_model} {python_path} -m agentwire.stt.stt_server"

    # Create tmux session
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name, "-c", run_dir
    ], check=True)

    subprocess.run([
        "tmux", "send-keys", "-t", session_name, cmd, "Enter"
    ], check=True)

    print(f"STT server starting in tmux session '{session_name}'")
    print(f"  Model: {model}")
    print(f"  Port: {port}")
    print(f"  Attach: tmux attach -t {session_name}")
    return 0


def cmd_stt_serve(args) -> int:
    """Run the STT server directly (foreground)."""
    import uvicorn

    port = args.port or 8101
    host = args.host or "0.0.0.0"
    config = load_config()
    stt_config = config.get("stt", {})
    model = args.model or stt_config.get("model", "base")
    backend = getattr(args, 'backend', None) or stt_config.get("engine", "auto")
    moonshine_model = stt_config.get("moonshine_model", "moonshine/base")

    os.environ["WHISPER_MODEL"] = model
    os.environ["WHISPER_DEVICE"] = "cpu"
    os.environ["STT_BACKEND"] = backend
    os.environ["MOONSHINE_MODEL"] = moonshine_model

    print(f"Starting STT server on {host}:{port} with model {model}...")
    uvicorn.run(
        "agentwire.stt.stt_server:app",
        host=host,
        port=port,
        log_level="info",
    )
    return 0


def cmd_stt_stop(args) -> int:
    """Stop the STT server."""
    session_name = get_stt_session_name()

    if not tmux_session_exists(session_name):
        print("STT server is not running.")
        return 1

    subprocess.run(["tmux", "kill-session", "-t", session_name])
    print("STT server stopped.")
    return 0


def cmd_stt_status(args) -> int:
    """Report the active STT tier's path — only probing a server when the tier
    actually has one (default-with-Moonshine, custom). Cloud and the browser
    fallback have no shim to probe (#441)."""
    from .config import load_config as load_config_typed
    from .voice_status import resolve_stt_status

    json_mode = getattr(args, 'json', False)
    session_name = get_stt_session_name()
    st = resolve_stt_status(load_config_typed())

    if json_mode:
        payload = {"success": True, **st.to_json()}
        if st.server_url:
            payload["session"] = session_name if tmux_session_exists(session_name) else None
        _output_json(payload)
        return 0 if st.ready else 1

    print(f"STT: {st.tier} tier — {st.path}")
    print(f"  {'[ok]' if st.ready else '[!!]'} {st.detail}")
    if st.server_url and tmux_session_exists(session_name):
        print(f"  Attach: tmux attach -t {session_name}")
    if not st.ready and st.server_url:
        print("  Start: agentwire stt start")
    for w in st.warnings:
        print(f"  [..] {w}")
    return 0 if st.ready else 1


# === Kokoro (default-tier TTS shim) Commands ===

def cmd_kokoro_start(args) -> int:
    """Start the default-tier Kokoro TTS shim in tmux (idempotent, health-aware).

    Mirrors ``agentwire stt start``: the portal's ``ensure_managed_tts`` calls
    this on startup. An existing session is reused only when it is actually
    serving (``/health`` ok/warming); a dead-but-present session is reaped and
    relaunched so a wedged shim self-heals instead of masking forever (#734)."""
    session_name = get_kokoro_session_name()
    port = args.port or 8102
    host = args.host or "0.0.0.0"

    if tmux_session_exists(session_name):
        live, status = _shim_session_state(session_name, port)
        if live:
            print(f"Kokoro TTS shim already running in tmux session '{session_name}'")
            print(f"  Attach: tmux attach -t {session_name}")
            return 0
        # Session exists but :{port}/health isn't serving (dead/wedged) —
        # self-heal by reaping and relaunching instead of masking a dead
        # engine behind mere session existence (#734).
        print(
            f"Kokoro TTS shim session '{session_name}' is present but not serving "
            f"(state: {status or 'no response'}) — relaunching."
        )
        _reap_shim_session(session_name)

    # Resolve an interpreter that can run the shim: dev-checkout venv when one
    # exists, otherwise the installed package's interpreter.
    python_path, cwd, error = _resolve_shim_python()
    if error:
        print(f"Error: cannot start the Kokoro shim — {error}", file=sys.stderr)
        return 1
    run_dir = cwd or str(Path.home())

    cmd = f"cd {run_dir} && KOKORO_PORT={port} KOKORO_HOST={host} {python_path} -m agentwire.tts.kokoro_server"

    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name, "-c", run_dir
    ], check=True)

    subprocess.run([
        "tmux", "send-keys", "-t", session_name, cmd, "Enter"
    ], check=True)

    print(f"Kokoro TTS shim starting in tmux session '{session_name}'")
    print(f"  Port: {port}")
    print(f"  Attach: tmux attach -t {session_name}")
    return 0


def cmd_kokoro_serve(args) -> int:
    """Run the Kokoro TTS shim directly (foreground)."""
    import uvicorn

    port = args.port or 8102
    host = args.host or "0.0.0.0"

    os.environ["KOKORO_PORT"] = str(port)
    os.environ["KOKORO_HOST"] = host

    print(f"Starting Kokoro TTS shim on {host}:{port}...")
    uvicorn.run(
        "agentwire.tts.kokoro_server:app",
        host=host,
        port=port,
        log_level="info",
    )
    return 0


def cmd_kokoro_stop(args) -> int:
    """Stop the Kokoro TTS shim."""
    session_name = get_kokoro_session_name()

    if not tmux_session_exists(session_name):
        print("Kokoro TTS shim is not running.")
        return 1

    subprocess.run(["tmux", "kill-session", "-t", session_name])
    print("Kokoro TTS shim stopped.")
    return 0


def cmd_kokoro_status(args) -> int:
    """Check Kokoro TTS shim status."""
    json_mode = getattr(args, 'json', False)
    session_name = get_kokoro_session_name()
    config = load_config()
    kokoro_url = config.get("tts", {}).get("url") or "http://localhost:8102"

    probe_error = None
    try:
        req = urllib.request.Request(f"{kokoro_url}/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            if json_mode:
                _output_json({
                    "success": True,
                    "running": True,
                    "url": kokoro_url,
                    "healthy": data.get("status") == "ok",
                    "status": data.get("status", "unknown"),
                    "percent": data.get("percent", 0),
                    "session": session_name if tmux_session_exists(session_name) else None,
                })
            else:
                print("Kokoro TTS shim is running")
                print(f"  State: {data.get('status', 'unknown')} ({data.get('percent', 0)}%)")
                print(f"  URL: {kokoro_url}")
                if tmux_session_exists(session_name):
                    print(f"  Attach: tmux attach -t {session_name}")
            return 0
    except Exception as e:
        probe_error = _describe_probe_error(e)

    if tmux_session_exists(session_name):
        if json_mode:
            _output_json({
                "success": True,
                "running": True,
                "url": kokoro_url,
                "healthy": False,
                "error": probe_error,
                "session": session_name,
                "starting": True,
            })
        else:
            print(f"Kokoro TTS shim is starting in tmux session '{session_name}'")
            if probe_error:
                print(f"  Probe: {probe_error}")
            print(f"  Attach: tmux attach -t {session_name}")
        return 0

    if json_mode:
        _output_json({
            "success": True,
            "running": False,
            "url": kokoro_url,
            "healthy": False,
            "error": probe_error,
        })
    else:
        print("Kokoro TTS shim is not running.")
        if probe_error:
            print(f"  Probe: {probe_error}")
        print("  Start: agentwire kokoro start")
    return 1


# === TTS restart helpers (server lifecycle; the say path retries through these) ===


def _restart_tts_local_for_venv(venv: str, backend: str) -> bool:
    """Restart local TTS server with specific venv (non-interactive).

    Returns True if restart succeeded, False otherwise.
    """
    import time
    session_name = get_tts_session_name()
    config = load_config()
    tts_config = config.get("tts", {})
    port = tts_config.get("port", 8100)
    host = tts_config.get("host", "0.0.0.0")

    # Stop existing server
    if tmux_session_exists(session_name):
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
        time.sleep(1)

    # Find source directory and venv
    source_dir = get_source_dir()
    if not source_dir:
        print("Error: Cannot find agentwire source directory.", file=sys.stderr)
        return False

    venv_name = f".venv-{venv}"
    venv_path = source_dir / venv_name / "bin" / "activate"

    if not venv_path.exists():
        print(f"Error: Venv not found: {venv_path}", file=sys.stderr)
        return False

    # Start server in tmux (non-interactive)
    tts_cmd = (
        f"cd {source_dir} && "
        f"source {venv_name}/bin/activate && "
        f"python -m agentwire tts serve --host {host} --port {port} --backend {backend} --venv {venv}"
    )

    subprocess.run(["tmux", "new-session", "-d", "-s", session_name], capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", session_name, tts_cmd, "Enter"], capture_output=True)

    # Wait for server to be ready
    url = f"http://{host}:{port}/health"
    for _ in range(30):  # Wait up to 30 seconds
        time.sleep(1)
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass

    print("Warning: Server may not be fully ready yet.", file=sys.stderr)
    return True  # Continue anyway, it might just be slow to load models


def _restart_tts_remote_for_venv(ssh_target: str, machine_id: str, venv: str, backend: str) -> bool:
    """Restart remote TTS server with specific venv/backend.

    Returns True if restart succeeded, False otherwise.
    """
    import time
    session_name = get_tts_session_name()
    config = load_config()
    tts_config = config.get("tts", {})
    port = tts_config.get("port", 8100)

    # Stop existing server
    kill_cmd = f"tmux kill-session -t {session_name} 2>/dev/null || true"
    subprocess.run(["ssh", ssh_target, kill_cmd], capture_output=True)
    time.sleep(1)

    # Start with new backend (venv is determined by backend on remote)
    # Use agentwire tts start which handles venv selection
    start_cmd = f"~/.local/bin/agentwire tts start --backend {backend}"
    result = subprocess.run(
        ["ssh", ssh_target, start_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0 and "already running" not in result.stdout:
        # Check if it failed for a real reason (not just "already running")
        if "not a terminal" not in result.stdout and "not a terminal" not in result.stderr:
            print(f"Failed to start TTS on {machine_id}: {result.stderr}", file=sys.stderr)
            return False

    # Wait for server to be ready (check via tunnel)
    url = f"http://localhost:{port}/health"
    for _ in range(60):  # Wait up to 60 seconds for remote (models take time)
        time.sleep(1)
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass

    print("Warning: Remote server may not be fully ready yet.", file=sys.stderr)
    return True  # Continue anyway


def _restart_tts_for_venv(venv: str, backend: str) -> bool:
    """Restart TTS server with specific venv (non-interactive).

    Handles both local and remote TTS servers.
    Returns True if restart succeeded, False otherwise.
    """
    from .network import NetworkContext

    ctx = NetworkContext.from_config()

    if ctx.is_local("tts"):
        return _restart_tts_local_for_venv(venv, backend)

    # Remote TTS
    ssh_target = ctx.get_ssh_target("tts")
    machine_id = ctx.get_machine_for_service("tts")

    if not ssh_target or not machine_id:
        print("TTS configured for remote machine but machine not found.", file=sys.stderr)
        return False

    print(f"Restarting TTS on {machine_id} with backend '{backend}'...")
    return _restart_tts_remote_for_venv(ssh_target, machine_id, venv, backend)


# === Voice listing ===


def cmd_tts_voices(args) -> int:
    """List available TTS voices (custom-shim voices, or Kokoro presets)."""
    json_mode = getattr(args, 'json', False)

    import requests

    tts_config = load_config().get("tts", {})
    is_custom = tts_config.get("backend") == "custom"

    if not is_custom:
        # Default tier speaks via Kokoro presets — no cloning, but surface
        # the preset list so agents/users know what voices exist.
        try:
            from .tts.engines.kokoro import PRESET_VOICES
            presets = list(PRESET_VOICES)
        except ImportError:
            presets = []
        if json_mode:
            _output_json({"success": True, "voices": [], "preset_voices": presets})
        else:
            print("No custom voices — cloned voices require tts.backend: custom.")
            if presets:
                print(f"Default tier speaks via Kokoro. Preset voices: {', '.join(presets)}")
        return 0

    tts_url = tts_config.get("url")
    try:
        response = requests.get(f"{tts_url}/voices", timeout=10)
        if response.status_code == 200:
            data = response.json()
            voices = data.get("voices", data) if isinstance(data, dict) else data
            if json_mode:
                _output_json({"success": True, "voices": voices or []})
            else:
                if not voices:
                    print("No voices available")
                    return 0
                print(f"Available voices ({len(voices)}):")
                for v in sorted(voices, key=lambda x: x.get("name", "")):
                    name = v.get("name", "?")
                    duration = v.get("duration", "?")
                    print(f"  {name}: {duration}s")
            return 0
        else:
            if json_mode:
                _output_json({"success": False, "error": f"HTTP {response.status_code}"})
            else:
                print(f"Failed to list voices: {response.status_code}")
            return 1
    except requests.RequestException as e:
        if json_mode:
            _output_json({"success": False, "error": str(e)})
        else:
            print(f"Connection failed: {e}")
        return 1


# === Parser registration ===

_TTS_BACKEND_CHOICES = [
    "kokoro", "chatterbox", "chatterbox-streaming",
    "zonos-transformer", "zonos-hybrid",
]


def register_tts_parser(subparsers) -> None:
    # === tts command group ===
    tts_parser = subparsers.add_parser("tts", help="Manage TTS server")
    tts_subparsers = tts_parser.add_subparsers(dest="tts_command")

    # tts start
    tts_start = tts_subparsers.add_parser("start", help="Start TTS server in tmux")
    tts_start.add_argument("--port", type=int, help="Server port (default: 8100)")
    tts_start.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    tts_start.add_argument("--backend", type=str,
                           choices=_TTS_BACKEND_CHOICES,
                           help="TTS backend (default: chatterbox)")
    tts_start.set_defaults(func=cmd_tts_start)

    # tts serve (run in foreground)
    tts_serve = tts_subparsers.add_parser("serve", help="Run TTS server in foreground")
    tts_serve.add_argument("--port", type=int, help="Server port (default: 8100)")
    tts_serve.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    tts_serve.add_argument("--backend", type=str,
                           choices=_TTS_BACKEND_CHOICES,
                           help="TTS backend (default: chatterbox)")
    tts_serve.add_argument("--venv", type=str,
                           choices=["kokoro", "chatterbox", "zonos"],
                           help="Which venv family is running (for hot-swap detection)")
    tts_serve.set_defaults(func=cmd_tts_serve)

    # tts stop
    tts_stop = tts_subparsers.add_parser("stop", help="Stop TTS server")
    tts_stop.set_defaults(func=cmd_tts_stop)

    # tts restart
    tts_restart = tts_subparsers.add_parser("restart", help="Restart TTS server (with optional venv switch)")
    tts_restart.add_argument("--port", type=int, help="Server port (default: 8100)")
    tts_restart.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    tts_restart.add_argument("--backend", type=str,
                             choices=_TTS_BACKEND_CHOICES,
                             help="TTS backend")
    tts_restart.add_argument("--venv", type=str,
                             choices=["kokoro", "chatterbox", "zonos"],
                             help="Force specific venv family")
    tts_restart.set_defaults(func=cmd_tts_restart)

    # tts status
    tts_status = tts_subparsers.add_parser("status", help="Check TTS status")
    tts_status.add_argument("--json", action="store_true", help="Output JSON")
    tts_status.set_defaults(func=cmd_tts_status)

    # tts warm
    tts_warm = tts_subparsers.add_parser(
        "warm",
        help="Download the default-tier Kokoro voice model (~200 MB) and verify it loads",
    )
    tts_warm.set_defaults(func=cmd_tts_warm)

    # tts voices
    tts_voices = tts_subparsers.add_parser(
        "voices", help="List available TTS voices (custom-shim voices or Kokoro presets)"
    )
    tts_voices.add_argument("--json", action="store_true", help="Output JSON")
    tts_voices.set_defaults(func=cmd_tts_voices)

    # === stt command group ===
    stt_parser = subparsers.add_parser("stt", help="Manage STT server (native Whisper)")
    stt_subparsers = stt_parser.add_subparsers(dest="stt_command")

    # stt start
    stt_start = stt_subparsers.add_parser("start", help="Start STT server in tmux")
    stt_start.add_argument("--port", type=int, help="Server port (default: 8101)")
    stt_start.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    stt_start.add_argument("--model", type=str, help="Whisper model (tiny/base/small/medium/large-v3)")
    stt_start.add_argument("--backend", type=str, help="STT backend: auto (default), moonshine, whisper")
    stt_start.set_defaults(func=cmd_stt_start)

    # stt serve
    stt_serve = stt_subparsers.add_parser("serve", help="Run STT server in foreground")
    stt_serve.add_argument("--port", type=int, help="Server port (default: 8101)")
    stt_serve.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    stt_serve.add_argument("--model", type=str, help="Whisper model (tiny/base/small/medium/large-v3)")
    stt_serve.add_argument("--backend", type=str, help="STT backend: auto (default), moonshine, whisper")
    stt_serve.set_defaults(func=cmd_stt_serve)

    # stt stop
    stt_stop = stt_subparsers.add_parser("stop", help="Stop STT server")
    stt_stop.set_defaults(func=cmd_stt_stop)

    # stt status
    stt_status = stt_subparsers.add_parser("status", help="Check STT status")
    stt_status.add_argument("--json", action="store_true", help="Output JSON")
    stt_status.set_defaults(func=cmd_stt_status)

    # === kokoro command group (default-tier TTS shim) ===
    kokoro_parser = subparsers.add_parser(
        "kokoro", help="Manage the default-tier Kokoro TTS shim (process-isolated)"
    )
    kokoro_subparsers = kokoro_parser.add_subparsers(dest="kokoro_command")

    # kokoro start
    kokoro_start = kokoro_subparsers.add_parser("start", help="Start Kokoro shim in tmux")
    kokoro_start.add_argument("--port", type=int, help="Server port (default: 8102)")
    kokoro_start.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    kokoro_start.set_defaults(func=cmd_kokoro_start)

    # kokoro serve
    kokoro_serve = kokoro_subparsers.add_parser("serve", help="Run Kokoro shim in foreground")
    kokoro_serve.add_argument("--port", type=int, help="Server port (default: 8102)")
    kokoro_serve.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    kokoro_serve.set_defaults(func=cmd_kokoro_serve)

    # kokoro stop
    kokoro_stop = kokoro_subparsers.add_parser("stop", help="Stop Kokoro shim")
    kokoro_stop.set_defaults(func=cmd_kokoro_stop)

    # kokoro status
    kokoro_status = kokoro_subparsers.add_parser("status", help="Check Kokoro shim status")
    kokoro_status.add_argument("--json", action="store_true", help="Output JSON")
    kokoro_status.set_defaults(func=cmd_kokoro_status)
