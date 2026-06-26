"""
SSH tunnel management for AgentWire.

Creates and monitors SSH tunnels to reach remote services.
Handles automatic creation, health checking, and cleanup of SSH port forwards.
"""

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .network import NetworkContext, TunnelSpec
from .ssh import ssh_base_opts, ssh_control_exit
from .utils.paths import agentwire_dir
from .utils.subprocess import run_command


@dataclass
class TunnelStatus:
    """Status of a tunnel."""

    spec: TunnelSpec
    status: str  # "up", "down", "error"
    pid: Optional[int] = None
    error: Optional[str] = None
    latency_ms: Optional[int] = None


class TunnelManager:
    """Manages SSH tunnels for reaching remote services."""

    def __init__(self, state_dir: Optional[Path] = None):
        """
        Initialize tunnel manager.

        Args:
            state_dir: Directory to store tunnel state (PIDs, etc.)
                      Defaults to ~/.agentwire/tunnels/
        """
        self.state_dir = state_dir or (agentwire_dir() / "tunnels")
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _get_state_file(self, spec: TunnelSpec) -> Path:
        """Get path to state file for a tunnel."""
        return self.state_dir / f"{spec.id}.json"

    def _get_ssh_target(self, machine_id: str, ctx: Optional[NetworkContext] = None) -> Optional[str]:
        """Get SSH target (user@host) for a machine."""
        if ctx is None:
            ctx = NetworkContext.from_config()

        machine = ctx.machines.get(machine_id)
        if machine is None:
            return None

        host = machine.get("host", machine_id)
        user = machine.get("user")

        if user:
            return f"{user}@{host}"
        return host

    def check_tunnel(self, spec: TunnelSpec) -> TunnelStatus:
        """
        Check if a tunnel is running.

        Checks:
        1. State file exists with PID
        2. Process is actually running
        3. Local port is listening (optional)

        Returns:
            TunnelStatus with current state
        """
        state_file = self._get_state_file(spec)

        if not state_file.exists():
            return TunnelStatus(spec=spec, status="down")

        try:
            state = json.loads(state_file.read_text())
            pid = state.get("pid")

            if pid is None:
                return TunnelStatus(spec=spec, status="down")

            # Check if process is running
            try:
                os.kill(pid, 0)  # Signal 0 just checks if process exists
            except (OSError, ProcessLookupError):
                # Process not running, clean up state file
                state_file.unlink(missing_ok=True)
                return TunnelStatus(spec=spec, status="down")

            # Process is running
            return TunnelStatus(spec=spec, status="up", pid=pid)

        except (json.JSONDecodeError, IOError) as e:
            return TunnelStatus(spec=spec, status="error", error=str(e))

    def create_tunnel(self, spec: TunnelSpec, ctx: Optional[NetworkContext] = None) -> TunnelStatus:
        """
        Create an SSH tunnel.

        Uses: ssh -L local_port:localhost:remote_port -N -f user@host

        Args:
            spec: Tunnel specification
            ctx: Optional NetworkContext (loaded if not provided)

        Returns:
            TunnelStatus with result
        """
        # Check if already running
        existing = self.check_tunnel(spec)
        if existing.status == "up":
            return existing

        # Get SSH target
        ssh_target = self._get_ssh_target(spec.remote_machine, ctx)
        if ssh_target is None:
            return TunnelStatus(
                spec=spec,
                status="error",
                error=f"Machine '{spec.remote_machine}' not found in machines.json",
            )

        # Build SSH command
        # ssh_base_opts() supplies ControlMaster multiplexing + keepalives
        # (ServerAliveInterval/CountMax). -L local_port:localhost:remote_port
        # port-forwards; -N runs no remote command; -f backgrounds;
        # -o ExitOnForwardFailure=yes exits if the forward can't bind.
        cmd = [
            "ssh",
            *ssh_base_opts(),
            "-L", f"{spec.local_port}:localhost:{spec.remote_port}",
            "-N",
            "-f",
            "-o", "ExitOnForwardFailure=yes",
            ssh_target,
        ]

        try:
            result = run_command(cmd, timeout=30)

            if not result.success:
                return TunnelStatus(
                    spec=spec,
                    status="error",
                    error=result.stderr.strip() or f"SSH exited with code {result.returncode}",
                )

            # Find the PID of the SSH process
            # The -f flag backgrounds the process, so we need to wait and retry
            pid = None
            for attempt in range(5):
                time.sleep(0.3)
                pid = self._find_tunnel_pid(spec)
                if pid:
                    break

            if pid:
                # Save state
                state = {"pid": pid, "spec": {"local_port": spec.local_port, "remote_machine": spec.remote_machine, "remote_port": spec.remote_port}}
                self._get_state_file(spec).write_text(json.dumps(state))

                return TunnelStatus(spec=spec, status="up", pid=pid)
            else:
                return TunnelStatus(
                    spec=spec,
                    status="error",
                    error="SSH started but could not find process",
                )

        except Exception as e:
            return TunnelStatus(spec=spec, status="error", error=str(e))

    def _find_tunnel_pid(self, spec: TunnelSpec) -> Optional[int]:
        """Find the PID of an SSH tunnel process."""
        # Use pgrep to find SSH processes with our port forward
        try:
            # Pattern must not start with - (interpreted as option on Linux)
            # Look for: ssh ... -L <local_port>:localhost:<remote_port>
            pattern = f"ssh.*{spec.local_port}:localhost:{spec.remote_port}"
            result = run_command(["pgrep", "-f", pattern], timeout=5)

            if result.success and result.stdout.strip():
                # Return first matching PID
                pids = result.stdout.strip().split("\n")
                return int(pids[0])

        except Exception:
            pass

        return None

    def destroy_tunnel(self, spec: TunnelSpec) -> TunnelStatus:
        """
        Destroy an SSH tunnel.

        Args:
            spec: Tunnel specification

        Returns:
            TunnelStatus (should be "down" if successful)
        """
        status = self.check_tunnel(spec)

        if status.status == "down":
            return status

        if status.pid:
            try:
                os.kill(status.pid, signal.SIGTERM)
                # Wait a bit for graceful shutdown
                time.sleep(0.5)

                # Check if still running
                try:
                    os.kill(status.pid, 0)
                    # Still running, force kill
                    os.kill(status.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass  # Process is gone

            except (OSError, ProcessLookupError):
                pass  # Process already gone

        # Clean up state file
        self._get_state_file(spec).unlink(missing_ok=True)

        # Best-effort: close the multiplexing master for this target so it
        # doesn't linger past an explicit teardown. A no-op if no master
        # exists; the next command just re-handshakes if it was shared.
        try:
            ssh_target = self._get_ssh_target(spec.remote_machine)
            if ssh_target:
                ssh_control_exit(ssh_target)
        except Exception:
            pass

        return TunnelStatus(spec=spec, status="down")

    def ensure_tunnels(self, ctx: Optional[NetworkContext] = None) -> list[TunnelStatus]:
        """
        Ensure all required tunnels are running.

        Args:
            ctx: Optional NetworkContext (loaded if not provided)

        Returns:
            List of TunnelStatus for all required tunnels
        """
        if ctx is None:
            ctx = NetworkContext.from_config()

        results = []

        for spec in ctx.get_required_tunnels():
            status = self.check_tunnel(spec)

            if status.status != "up":
                status = self.create_tunnel(spec, ctx)

            results.append(status)

        return results

    def list_tunnels(self) -> list[TunnelStatus]:
        """
        List all known tunnels from state files.

        Returns:
            List of TunnelStatus for all tracked tunnels
        """
        results = []

        for state_file in self.state_dir.glob("*.json"):
            try:
                state = json.loads(state_file.read_text())
                spec_data = state.get("spec", {})

                spec = TunnelSpec(
                    local_port=spec_data.get("local_port", 0),
                    remote_machine=spec_data.get("remote_machine", "unknown"),
                    remote_port=spec_data.get("remote_port", 0),
                )

                status = self.check_tunnel(spec)
                results.append(status)

            except (json.JSONDecodeError, IOError):
                continue

        return results

    def destroy_all_tunnels(self) -> int:
        """
        Destroy all managed tunnels.

        Returns:
            Number of tunnels destroyed
        """
        count = 0
        for status in self.list_tunnels():
            if status.status == "up":
                self.destroy_tunnel(status.spec)
                count += 1
        return count


def test_ssh_connectivity(host: str, user: Optional[str] = None, timeout: int = 5) -> Optional[int]:
    """
    Test SSH connectivity to a host.

    Args:
        host: Hostname or IP
        user: Optional username
        timeout: Connection timeout in seconds

    Returns:
        Latency in milliseconds, or None if unreachable
    """
    target = f"{user}@{host}" if user else host

    start = time.time()
    try:
        result = run_command(
            [
                "ssh",
                *ssh_base_opts(),
                "-o", f"ConnectTimeout={timeout}",
                # Do NOT TOFU-accept unknown host keys (was accept-new): an
                # unrecognised host is treated as unreachable, not silently
                # trusted. Hosts must be in known_hosts to probe as healthy.
                "-o", "StrictHostKeyChecking=yes",
                "-o", "BatchMode=yes",
                target,
                "echo ok",
            ],
            timeout=timeout + 2,
        )

        if result.success:
            elapsed = time.time() - start
            return int(elapsed * 1000)

    except Exception:
        pass

    return None


def test_service_health(url: str, timeout: int = 5) -> tuple[bool, Optional[str]]:
    """
    Test if a service is responding.

    Args:
        url: Full URL to health endpoint
        timeout: Request timeout in seconds

    Returns:
        Tuple of (is_healthy, error_message)
    """
    import ssl
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    from .security import is_loopback_host

    try:
        ctx = ssl.create_default_context()
        # Only disable TLS verification for loopback targets (self-signed local
        # dev certs). Remote health URLs get full certificate + hostname
        # verification — no blanket CERT_NONE (CWE-295).
        host = urlparse(url).hostname or ""
        if is_loopback_host(host):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
            if response.status == 200:
                return True, None
            return False, f"HTTP {response.status}"

    except urllib.error.URLError as e:
        return False, str(e.reason)
    except Exception as e:
        return False, str(e)
