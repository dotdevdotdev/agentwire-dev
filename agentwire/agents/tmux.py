"""Tmux-based agent backend."""

import json
import logging
import os
import shlex
import subprocess
from pathlib import Path

from ..ssh import ssh_base_opts
from .base import AgentBackend

logger = logging.getLogger(__name__)

# Base command without permission flags - flags added based on bypass_permissions option
DEFAULT_AGENT_COMMAND = "claude"


def _tmux_path() -> str:
    """Resolve tmux binary path, with fallback to common locations."""
    import shutil
    return shutil.which("tmux") or "/opt/homebrew/bin/tmux"


def tmux_session_exists(name: str) -> bool:
    """Check if a local tmux session exists (exact match).

    Module-level helper for use outside the TmuxAgent class.
    """
    result = subprocess.run(
        [_tmux_path(), "has-session", "-t", f"={name}"],
        capture_output=True,
    )
    return result.returncode == 0


class TmuxAgent(AgentBackend):
    """Agent backend using tmux sessions."""

    def __init__(self, config: dict):
        """Initialize TmuxAgent.

        Args:
            config: Configuration dict with optional keys:
                - agent.command: Command to start agent (default: claude --dangerously-skip-permissions)
                - agent.model: Model to use (for {model} placeholder)
                - machines.file: Path to machines.json
        """
        self.config = config
        agent_config = config.get("agent", {})
        self.agent_command = agent_config.get("command", DEFAULT_AGENT_COMMAND)
        self.default_model = agent_config.get("model", "")

        # Load machines from file
        self._load_machines()

    def _load_machines(self):
        """Load machines configuration from file."""
        machines_config = self.config.get("machines", {})
        machines_file = machines_config.get("file")

        if machines_file:
            machines_path = Path(machines_file).expanduser()
            logger.info(f"Loading machines from {machines_path}")
            if machines_path.exists():
                try:
                    with open(machines_path) as f:
                        data = json.load(f)
                        self.machines = data.get("machines", [])
                        machine_ids = [m.get("id") for m in self.machines]
                        logger.info(f"Loaded {len(self.machines)} machines: {machine_ids}")
                        return
                except (json.JSONDecodeError, IOError) as e:
                    logger.warning(f"Failed to load machines: {e}")
            else:
                logger.warning(f"Machines file not found: {machines_path}")
        else:
            logger.info("No machines.file configured - using local tmux only")

        self.machines = []

    def _run_local(self, cmd: list[str], capture: bool = True) -> subprocess.CompletedProcess:
        """Run a command locally.

        Args:
            cmd: Command as list of strings
            capture: Whether to capture output

        Returns:
            CompletedProcess result
        """
        logger.debug(f"Running local: {' '.join(cmd)}")
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
        )

    def _run_remote(self, machine: dict, cmd: str, capture: bool = True) -> subprocess.CompletedProcess:
        """Run a command on a remote machine via SSH.

        Args:
            machine: Machine config dict with 'host' and optional 'user'
            cmd: Command string to run remotely
            capture: Whether to capture output

        Returns:
            CompletedProcess result
        """
        host = machine.get("host", "")
        user = machine.get("user", "")

        ssh_target = f"{user}@{host}" if user else host
        port = machine.get("port")
        ssh_cmd = [
            "ssh",
            *ssh_base_opts(),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
        ]
        if port:
            ssh_cmd.extend(["-p", str(port)])
        ssh_cmd.extend([ssh_target, cmd])

        logger.debug(f"Running remote on {ssh_target}: {cmd}")
        return subprocess.run(
            ssh_cmd,
            capture_output=capture,
            text=True,
        )

    def _parse_session_name(self, name: str) -> tuple[str, dict | None]:
        """Parse session name to extract machine info.

        Args:
            name: Session name, optionally with @machine suffix

        Returns:
            Tuple of (session_name, machine_config or None for local)
        """
        import socket
        local_hostname = socket.gethostname().split('.')[0]
        in_container = os.path.exists('/.dockerenv')

        if "@" in name:
            session, machine_id = name.rsplit("@", 1)

            # Check if machine_id is the local hostname (only when not in container)
            if not in_container and (machine_id == local_hostname or machine_id == "local"):
                return session, None

            for machine in self.machines:
                if machine.get("id") == machine_id or machine.get("host") == machine_id:
                    # Check if this machine is marked as local (only when not in container)
                    # In Docker, we still need to SSH to "local" machines via host.docker.internal
                    if not in_container and machine.get("local"):
                        logger.debug(f"Resolved {name} -> session={session}, machine marked as local")
                        return session, None
                    logger.debug(f"Resolved {name} -> session={session}, machine_id={machine_id}")
                    return session, machine
            logger.warning(f"Unknown machine: {machine_id} (available: {[m.get('id') for m in self.machines]}), treating as local")
            return name, None
        return name, None

    def session_exists(self, name: str) -> bool:
        """Check if a tmux session exists."""
        session_name, machine = self._parse_session_name(name)

        if machine:
            cmd = f"tmux has-session -t {shlex.quote(session_name)} 2>/dev/null"
            result = self._run_remote(machine, cmd)
        else:
            result = self._run_local([
                "tmux", "has-session", "-t", session_name,
            ])

        return result.returncode == 0

    def get_output(self, name: str, lines: int = 50) -> str:
        """Get recent output from a tmux session with ANSI colors."""
        session_name, machine = self._parse_session_name(name)

        if machine:
            cmd = f"tmux capture-pane -t {shlex.quote(session_name)} -p -e -S -{lines}"
            result = self._run_remote(machine, cmd)
        else:
            result = self._run_local([
                "tmux", "capture-pane",
                "-t", session_name,
                "-p",  # Print to stdout
                "-e",  # Include ANSI escape sequences
                "-S", f"-{lines}",  # Start from N lines back
            ])

        if result.returncode != 0:
            logger.error(f"Failed to get output: {result.stderr}")
            return ""

        return result.stdout

    def send_keys(self, name: str, keys: str) -> bool:
        """Send keys to a tmux session WITHOUT Enter.

        Use this for keypresses like selecting menu options.
        For text input followed by Enter, use send_input instead.
        """
        session_name, machine = self._parse_session_name(name)

        if machine:
            cmd = f"tmux send-keys -t {shlex.quote(session_name)} -l {shlex.quote(keys)}"
            result = self._run_remote(machine, cmd)
        else:
            result = self._run_local([
                "tmux", "send-keys",
                "-t", session_name,
                "-l", keys,
            ])

        if result.returncode != 0:
            logger.error(f"Failed to send keys: {result.stderr}")
            return False

        return True

    def send_input(self, name: str, text: str) -> bool:
        """Send input to a tmux session (text + Enter)."""
        import os
        import tempfile
        import time
        session_name, machine = self._parse_session_name(name)

        use_buffer = len(text) > 10 or "\n" in text

        if machine:
            if use_buffer:
                # Use base64 + load-buffer on remote to avoid PTY flooding
                import base64
                encoded = base64.b64encode(text.encode()).decode()
                # Pipe straight into the tmux buffer — no on-disk temp that could
                # expose message content or be pre-planted on the remote host
                cmd = (
                    f"echo {shlex.quote(encoded)} | base64 -d | tmux load-buffer - && "
                    f"tmux paste-buffer -t {shlex.quote(session_name)} && "
                    f"sleep 0.2 && "
                    f"tmux send-keys -t {shlex.quote(session_name)} Enter"
                )
            else:
                cmd = (
                    f"tmux send-keys -t {shlex.quote(session_name)} -l {shlex.quote(text)} && "
                    f"sleep 0.2 && "
                    f"tmux send-keys -t {shlex.quote(session_name)} Enter"
                )
            result = self._run_remote(machine, cmd)
        else:
            if use_buffer:
                # Write to temp file, load into tmux buffer, paste as single unit
                with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                    f.write(text)
                    temp_path = f.name
                try:
                    result = self._run_local(["tmux", "load-buffer", temp_path])
                    if result.returncode != 0:
                        logger.error(f"Failed to load buffer: {result.stderr}")
                        return False
                    result = self._run_local(["tmux", "paste-buffer", "-t", session_name])
                    if result.returncode != 0:
                        logger.error(f"Failed to paste buffer: {result.stderr}")
                        return False
                finally:
                    os.unlink(temp_path)
            else:
                result = self._run_local([
                    "tmux", "send-keys",
                    "-t", session_name,
                    "-l", text,
                ])
                if result.returncode != 0:
                    logger.error(f"Failed to send input: {result.stderr}")
                    return False

            # Small delay before Enter
            time.sleep(0.2)

            # Send Enter separately
            result = self._run_local([
                "tmux", "send-keys",
                "-t", session_name,
                "Enter",
            ])

        if result.returncode != 0:
            logger.error(f"Failed to send input: {result.stderr}")
            return False

        return True

    def list_sessions(self) -> list[str]:
        """List all tmux sessions (from configured machines via SSH)."""
        sessions = []

        # Check if running in Docker container (portal-only mode)
        in_container = os.path.exists('/.dockerenv')

        # Always query local tmux
        # Use "local" as machine ID in container, hostname on host
        result = self._run_local([
            "tmux", "list-sessions", "-F", "#{session_name}",
        ])
        if result.returncode == 0 and result.stdout.strip():
            if in_container:
                local_machine_id = "local"
            else:
                import socket
                local_machine_id = socket.gethostname().split('.')[0]
            for name in result.stdout.strip().split("\n"):
                if name:
                    sessions.append(f"{name}@{local_machine_id}")

        # Remote sessions from configured machines
        for machine in self.machines:
            machine_id = machine.get("id", machine.get("host", ""))

            # Skip "local" machine when running on host (prevents duplication)
            if not in_container and machine_id == "local":
                continue

            cmd = "tmux list-sessions -F '#{session_name}' 2>/dev/null"
            result = self._run_remote(machine, cmd)
            if result.returncode == 0 and result.stdout.strip():
                for name in result.stdout.strip().split("\n"):
                    if name:
                        sessions.append(f"{name}@{machine_id}")

        return sessions
