"""Shared SSH connection options — single source of truth for every ssh call.

ControlMaster multiplexing: the first ssh to a target opens a master connection
and parks its control socket under ``~/.ssh/sockets/``; subsequent ssh
invocations to the same ``user@host:port`` ride that socket and skip the TCP +
auth handshake entirely (~90-140ms on loopback, 300-500ms over a VPN — measured
in #297). ``ControlPersist`` keeps the idle master alive for a window after the
last client exits so a *burst* of remote ops pays the handshake once.

Degrades gracefully by design:
- Socket dir missing → we create it on demand (``ensure_socket_dir``).
- Master gone / never started → ssh just opens a fresh connection (the cost we
  pay today on every call anyway).
- Master wedged → ``ServerAliveInterval`` evicts it instead of hanging forever.

This mirrors Ansible's default (ControlMaster on out of the box), so the failure
modes are well-trodden.
"""

import os
import subprocess
from pathlib import Path

# How long an idle master lingers after its last client exits. A burst of
# commands within this window reuses the socket; after it the master self-exits,
# which is also how stale sockets clean themselves up.
CONTROL_PERSIST_SECONDS = 600

# One control socket per target. ``%r@%h-%p`` = remote-user @ host - port, so
# distinct targets never collide on the same socket. ssh expands these tokens
# itself; we hand it an absolute path (subprocess does not expand ``~``).
SSH_SOCKET_DIR = Path.home() / ".ssh" / "sockets"


def ensure_socket_dir() -> bool:
    """Create ``~/.ssh/sockets/`` (0700) on demand. Idempotent; never raises.

    Returns True if the dir exists and is usable afterward, False otherwise.

    Called from :func:`ssh_base_opts` so the dir exists before the first
    multiplexed connection. The return value matters: ``ControlMaster=auto``
    with a *missing* ``ControlPath`` directory does NOT degrade — ssh aborts
    with ``unix_listener: cannot bind`` (exit 255) and the command never runs.
    So when we can't guarantee the dir, the caller drops multiplexing entirely
    and falls back to a plain (re-handshaking) connection.
    """
    try:
        SSH_SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SSH_SOCKET_DIR, 0o700)
        return SSH_SOCKET_DIR.is_dir()
    except OSError:
        return False


def ssh_base_opts() -> list[str]:
    """Shared ``-o`` flags for every ssh invocation (SSOT).

    Returns ControlMaster multiplexing flags plus keepalives as a flat
    argv-ready list, meant to be spliced right after the ``"ssh"`` token::

        subprocess.run(["ssh", *ssh_base_opts(), target, command])

    Side effect: ensures the socket dir exists. If it can't be created (read-only
    home, perms) the ControlMaster flags are *omitted* so ssh connects normally
    instead of aborting on an unbindable socket path — graceful degradation to
    today's per-command-handshake behavior. Keepalives are always included.
    """
    keepalives = [
        # Evict a wedged master rather than block forever on a dead socket.
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
    ]
    if not ensure_socket_dir():
        return keepalives
    control_path = str(SSH_SOCKET_DIR / "%r@%h-%p")
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path}",
        "-o", f"ControlPersist={CONTROL_PERSIST_SECONDS}",
        *keepalives,
    ]


def ssh_control_exit(ssh_target: str) -> None:
    """Tear down the master connection for ``ssh_target`` (``ssh -O exit``).

    Best-effort: closes the parked socket so nothing lingers past an explicit
    teardown (e.g. destroying a tunnel). A no-op if no master exists. Never
    raises — a failed teardown just means ControlPersist will reap it later.
    """
    control_path = str(SSH_SOCKET_DIR / "%r@%h-%p")
    try:
        subprocess.run(
            ["ssh", "-o", f"ControlPath={control_path}", "-O", "exit", ssh_target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass
