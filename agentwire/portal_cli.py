"""CLI for the web portal — ``agentwire portal ...``.

Start/stop/status/restart the portal web server (local tmux session or a
remote machine over SSH), plus the auth-token and device-pairing surface.
Shared, stateless helpers (``_start_portal_local``, ``_check_portal_health``,
``_default_portal_url``, ...) live in ``core``; the portal-private remote-spawn
and curl helpers travel with this module.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from .core import (
    _check_portal_health,
    _check_tmux_installed,
    _default_portal_url,
    _output_json,
    _start_portal_local,
    get_portal_session_name,
    load_config,
    tmux_session_exists,
)


def _portal_auth_curl_args() -> list[str]:
    """curl args carrying the portal auth token, if one is configured."""
    from .security import get_local_portal_token

    token = get_local_portal_token()
    return ["-H", f"Authorization: Bearer {token}"] if token else []


def _portal_api(method: str, path: str, data: dict | None = None, timeout: int = 10) -> dict | None:
    """Make an API request to the portal using curl (reliable with self-signed certs).

    Returns parsed JSON dict on success, None on failure.
    """
    config = load_config()
    portal_url = config.get("portal", {}).get("url", _default_portal_url())
    url = f"{portal_url}{path}"

    cmd = ["curl", "-sk", "--max-time", str(timeout), "-o", "-", "-w", "\n%{http_code}"]
    cmd.extend(_portal_auth_curl_args())
    if method == "POST":
        cmd.extend(["-X", "POST", "-H", "Content-Type: application/json"])
        if data is not None:
            cmd.extend(["-d", json.dumps(data)])
    elif method == "DELETE":
        cmd.extend(["-X", "DELETE"])
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        if result.returncode != 0:
            return None
        lines = result.stdout.rsplit("\n", 1)
        if len(lines) < 2:
            return None
        body, status_code = lines[0], lines[1].strip()
        if not status_code.startswith("2"):
            return None
        return json.loads(body) if body.strip() else {}
    except Exception:
        return None


def _start_portal_remote(ssh_target: str, machine_id: str, args) -> int:
    """Start portal on remote machine via SSH."""
    session_name = get_portal_session_name()

    # Check if portal already running remotely
    check_cmd = f"tmux has-session -t ={session_name} 2>/dev/null && echo running || echo stopped"
    result = subprocess.run(
        ["ssh", ssh_target, check_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Cannot reach portal machine. Check: ssh {ssh_target} echo ok", file=sys.stderr)
        return 1

    if "running" in result.stdout:
        print(f"Portal already running on {machine_id} in tmux session '{session_name}'")
        return 0

    # Build remote command
    if getattr(args, 'dev', False):
        cmd_parts = ["uv", "run", "python", "-m", "agentwire", "portal", "serve"]
    else:
        cmd_parts = ["agentwire", "portal", "serve"]

    if args.port:
        cmd_parts.extend(["--port", str(args.port)])
    if args.host:
        cmd_parts.extend(["--host", args.host])
    if args.no_tts:
        cmd_parts.append("--no-tts")
    if args.no_stt:
        cmd_parts.append("--no-stt")

    server_cmd = " ".join(cmd_parts)

    # Start remotely in tmux
    remote_cmd = f"tmux new-session -d -s {session_name} && tmux send-keys -t {session_name} {shlex.quote(server_cmd)} Enter"
    mode = "dev mode" if getattr(args, 'dev', False) else "installed"
    print(f"Starting AgentWire portal ({mode}) on {machine_id}...")

    result = subprocess.run(
        ["ssh", ssh_target, remote_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Failed to start portal on {machine_id}: {result.stderr}", file=sys.stderr)
        return 1

    print(f"Portal started on {machine_id}.")
    return 0


def _stop_portal_remote(ssh_target: str, machine_id: str) -> int:
    """Stop portal on remote machine via SSH."""
    session_name = get_portal_session_name()

    # Check if running
    check_cmd = f"tmux has-session -t ={session_name} 2>/dev/null && echo running || echo stopped"
    result = subprocess.run(
        ["ssh", ssh_target, check_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Cannot reach portal machine. Check: ssh {ssh_target} echo ok", file=sys.stderr)
        return 1

    if "stopped" in result.stdout:
        print(f"Portal is not running on {machine_id}.")
        return 1

    # Kill session
    kill_cmd = f"tmux kill-session -t {session_name}"
    result = subprocess.run(
        ["ssh", ssh_target, kill_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Failed to stop portal on {machine_id}: {result.stderr}", file=sys.stderr)
        return 1

    print(f"Portal stopped on {machine_id}.")
    return 0


def cmd_portal_start(args) -> int:
    """Start the AgentWire portal web server in tmux."""
    if not _check_tmux_installed():
        return 1

    from .network import NetworkContext

    ctx = NetworkContext.from_config()

    if ctx.is_local("portal"):
        return _start_portal_local(args)

    # Portal runs on another machine
    ssh_target = ctx.get_ssh_target("portal")
    machine_id = ctx.get_machine_for_service("portal")

    if not ssh_target or not machine_id:
        print("Portal configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    print(f"Portal runs on {machine_id}, starting remotely...")
    return _start_portal_remote(ssh_target, machine_id, args)


def cmd_portal_serve(args) -> int:
    """Run the web server directly (foreground)."""
    from .server import main as server_main

    server_main(
        config_path=str(args.config) if args.config else None,
        port=args.port,
        host=args.host,
        no_tts=args.no_tts,
        no_stt=args.no_stt,
    )
    return 0


def cmd_portal_stop(args) -> int:
    """Stop the AgentWire portal."""
    from .network import NetworkContext

    ctx = NetworkContext.from_config()
    session_name = get_portal_session_name()

    if ctx.is_local("portal"):
        if not tmux_session_exists(session_name):
            print("Portal is not running.")
            return 1

        subprocess.run(["tmux", "kill-session", "-t", session_name])
        print("Portal stopped.")
        return 0

    # Portal runs on another machine
    ssh_target = ctx.get_ssh_target("portal")
    machine_id = ctx.get_machine_for_service("portal")

    if not ssh_target or not machine_id:
        print("Portal configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    print(f"Portal runs on {machine_id}, stopping remotely...")
    return _stop_portal_remote(ssh_target, machine_id)


def cmd_portal_status(args) -> int:
    """Check portal status."""
    from .network import NetworkContext

    json_mode = getattr(args, 'json', False)
    ctx = NetworkContext.from_config()
    session_name = get_portal_session_name()

    if ctx.is_local("portal"):
        url = ctx.get_service_url("portal", use_tunnel=False)
        if tmux_session_exists(session_name):
            healthy = _check_portal_health(url)
            if json_mode:
                _output_json({
                    "success": True,
                    "running": True,
                    "url": url,
                    "session": session_name,
                    "healthy": healthy,
                    "machine": None,
                })
            else:
                print(f"Portal is running in tmux session '{session_name}'")
                print(f"  Attach: tmux attach -t {session_name}")
                if healthy:
                    print(f"  Health: OK ({url})")
                else:
                    print("  Health: starting or not responding yet")
            return 0
        else:
            if json_mode:
                _output_json({
                    "success": True,
                    "running": False,
                    "url": url,
                    "session": session_name,
                    "healthy": False,
                    "machine": None,
                })
            else:
                print("Portal is not running.")
                print("  Start:  agentwire portal start")
            return 1

    # Portal runs on another machine - check via health endpoint
    machine_id = ctx.get_machine_for_service("portal")
    url = ctx.get_service_url("portal", use_tunnel=True)

    healthy = _check_portal_health(url)
    if healthy:
        if json_mode:
            _output_json({
                "success": True,
                "running": True,
                "url": url,
                "healthy": True,
                "machine": machine_id,
            })
        else:
            print(f"Portal runs on {machine_id}")
            print("  Status: running")
            print(f"  Health: OK ({url})")
        return 0
    else:
        # Try direct connection if tunnel might not exist
        direct_url = ctx.get_service_url("portal", use_tunnel=False)
        if direct_url != url and _check_portal_health(direct_url):
            if json_mode:
                _output_json({
                    "success": True,
                    "running": True,
                    "url": direct_url,
                    "healthy": True,
                    "machine": machine_id,
                    "tunnel_issue": True,
                })
            else:
                print(f"Portal runs on {machine_id}")
                print("  Status: running (tunnel not working, direct OK)")
                print(f"  Health: OK ({direct_url})")
                print("  Hint: Run 'agentwire tunnels check' to verify tunnels")
            return 0

        if json_mode:
            _output_json({
                "success": True,
                "running": False,
                "url": url,
                "healthy": False,
                "machine": machine_id,
            })
        else:
            print(f"Portal runs on {machine_id}")
            print("  Status: not reachable")
            print(f"  Checked: {url}")
            if direct_url != url:
                print(f"  Also checked: {direct_url}")
        return 1


def cmd_portal_token(args) -> int:
    """Print (or rotate) the portal auth token."""
    from .security import (
        TOKEN_FILE,
        generate_token,
        get_local_portal_token,
        write_token_file,
    )

    config = load_config()
    override = config.get("server", {}).get("auth_token")

    if getattr(args, "rotate", False):
        token = generate_token()
        write_token_file(token)
        print(token)
        print(f"\nNew token written to {TOKEN_FILE}", file=sys.stderr)
        print(
            "Restart the portal (agentwire portal restart) and re-enter the "
            "token on remote devices.",
            file=sys.stderr,
        )
        if override:
            print(
                "Warning: server.auth_token is set in config.yaml and overrides "
                "the token file — rotation has no effect until it's removed.",
                file=sys.stderr,
            )
        return 0

    if override == "":
        print("Portal auth is disabled (server.auth_token: \"\" in config.yaml).", file=sys.stderr)
        return 1

    token = get_local_portal_token()
    if not token:
        print(
            "No token configured yet — it's generated on first portal start, "
            "or run `agentwire portal token --rotate` to create one now.",
            file=sys.stderr,
        )
        return 1

    print(token)
    if override:
        print("(from server.auth_token override in config.yaml)", file=sys.stderr)
    return 0


def cmd_portal_pair(args) -> int:
    """Create a short-lived pairing code (+ QR) for a new device."""
    from .devices import PAIRING_TTL_SECONDS, create_pairing

    pairing = create_pairing(name=getattr(args, "name", None) or "device")

    portal_url = _default_portal_url()
    pair_url = f"{portal_url}/pair?code={pairing.code}"
    ttl_min = PAIRING_TTL_SECONDS // 60

    print(f"Pairing code: {pairing.code}")
    print(f"  Name:  {pairing.name}")
    print(f"  Expires in {ttl_min} minutes.")
    print()
    print("On the device, open:")
    print(f"  {pair_url}")
    print("or visit", f"{portal_url}/pair", "and enter the code.")
    print()

    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(border=1)
        qr.add_data(pair_url)
        qr.make()
        qr.print_ascii(invert=True)
    except Exception:
        print("(install `qrcode` to render a scannable QR here)")

    return 0


def cmd_portal_devices(args) -> int:
    """List paired portal devices."""
    from .devices import DeviceRegistry

    registry = DeviceRegistry.load()
    json_mode = getattr(args, "json", False)

    if json_mode:
        _output_json({"success": True, "devices": [d.public() for d in registry.devices]})
        return 0

    if not registry.devices:
        print("No paired devices. The host bootstrap token (agentwire portal token)")
        print("is the only credential. Add one with: agentwire portal pair")
        return 0

    print(f"{'ID':<14}{'NAME':<24}{'LAST SEEN':<22}STATUS")
    for d in registry.devices:
        status = "revoked" if d.revoked else "active"
        print(
            f"{d.id:<14}{(d.name or '')[:23]:<24}"
            f"{(d.last_seen or 'never'):<22}{status}"
        )
    return 0


def cmd_portal_revoke(args) -> int:
    """Revoke one paired device without affecting the others."""
    from .devices import DeviceRegistry

    registry = DeviceRegistry.load()
    if registry.revoke(args.device_id):
        print(f"Revoked device '{args.device_id}'. It now gets 401 on every route.")
        return 0
    print(
        f"No active device with id '{args.device_id}'. "
        "List them with: agentwire portal devices",
        file=sys.stderr,
    )
    return 1


def cmd_portal_restart(args) -> int:
    """Restart the AgentWire portal (stop + start)."""
    import time

    print("Stopping portal...")
    stop_result = cmd_portal_stop(args)

    if stop_result != 0:
        # Portal wasn't running, just start it
        print("Portal was not running, starting fresh...")

    # Brief pause to ensure clean shutdown
    time.sleep(0.5)

    print("Starting portal...")
    return cmd_portal_start(args)


def register_portal_parser(subparsers) -> None:
    # ``portal generate-certs`` wires to cmd_generate_certs, which lives in
    # system_cli (not a portal-domain command). Deferred import at build time.
    from .system_cli import cmd_generate_certs

    portal_parser = subparsers.add_parser("portal", help="Manage the web portal")
    portal_subparsers = portal_parser.add_subparsers(dest="portal_command")

    # portal start
    portal_start = portal_subparsers.add_parser(
        "start", help="Start portal in tmux session"
    )
    portal_start.add_argument("--config", type=Path, help="Config file path")
    portal_start.add_argument("--port", type=int, help="Override port")
    portal_start.add_argument("--host", type=str, help="Override host")
    portal_start.add_argument("--no-tts", action="store_true", help="Disable TTS")
    portal_start.add_argument("--no-stt", action="store_true", help="Disable STT")
    portal_start.add_argument("--dev", action="store_true",
                              help="Run from source (uv run) - picks up code changes")
    portal_start.set_defaults(func=cmd_portal_start)

    # portal serve (run in foreground)
    portal_serve = portal_subparsers.add_parser(
        "serve", help="Run portal in foreground"
    )
    portal_serve.add_argument("--config", type=Path, help="Config file path")
    portal_serve.add_argument("--port", type=int, help="Override port")
    portal_serve.add_argument("--host", type=str, help="Override host")
    portal_serve.add_argument("--no-tts", action="store_true", help="Disable TTS")
    portal_serve.add_argument("--no-stt", action="store_true", help="Disable STT")
    portal_serve.set_defaults(func=cmd_portal_serve)

    # portal stop
    portal_stop = portal_subparsers.add_parser("stop", help="Stop the portal")
    portal_stop.set_defaults(func=cmd_portal_stop)

    # portal status
    portal_status = portal_subparsers.add_parser("status", help="Check portal status")
    portal_status.add_argument("--json", action="store_true", help="Output JSON")
    portal_status.set_defaults(func=cmd_portal_status)

    # portal restart
    portal_restart = portal_subparsers.add_parser("restart", help="Restart the portal (stop + start)")
    portal_restart.add_argument("--config", type=Path, help="Config file path")
    portal_restart.add_argument("--port", type=int, help="Override port")
    portal_restart.add_argument("--host", type=str, help="Override host")
    portal_restart.add_argument("--no-tts", action="store_true", help="Disable TTS")
    portal_restart.add_argument("--no-stt", action="store_true", help="Disable STT")
    portal_restart.add_argument("--dev", action="store_true",
                                help="Run from source (uv run) - picks up code changes")
    portal_restart.set_defaults(func=cmd_portal_restart)

    # portal generate-certs
    portal_certs = portal_subparsers.add_parser(
        "generate-certs", help="Generate SSL certificates"
    )
    portal_certs.set_defaults(func=cmd_generate_certs)

    # portal token
    portal_token = portal_subparsers.add_parser(
        "token", help="Print the portal auth token (required for non-loopback binds)"
    )
    portal_token.add_argument(
        "--rotate", action="store_true", help="Generate and save a new token"
    )
    portal_token.set_defaults(func=cmd_portal_token)

    # portal pair — mint a pairing code (+QR) for a new device
    portal_pair = portal_subparsers.add_parser(
        "pair", help="Pair a new device (prints a short-lived code + QR)"
    )
    portal_pair.add_argument("--name", help="Friendly device name (e.g. 'phone')")
    portal_pair.set_defaults(func=cmd_portal_pair)

    # portal devices — list paired devices
    portal_devices = portal_subparsers.add_parser(
        "devices", help="List paired portal devices"
    )
    portal_devices.add_argument("--json", action="store_true", help="Output JSON")
    portal_devices.set_defaults(func=cmd_portal_devices)

    # portal revoke — revoke one device
    portal_revoke = portal_subparsers.add_parser(
        "revoke", help="Revoke one paired device by id"
    )
    portal_revoke.add_argument("device_id", help="Device id (see `portal devices`)")
    portal_revoke.set_defaults(func=cmd_portal_revoke)
