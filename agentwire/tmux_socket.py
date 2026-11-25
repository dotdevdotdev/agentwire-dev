"""Where the tmux server socket lives, and servers that lost theirs.

A tmux server listens on ``<dir>/tmux-<uid>/default``, where ``<dir>`` is
``$TMUX_TMPDIR`` or ``/tmp``. Delete that socket file (anything that clears
``/tmp`` can) and the server keeps running: every session, pane and agent in it
is alive but unreachable. The next ``tmux`` call finds no server and starts a
NEW one on the same path, which looks like "tmux crashed and the sessions came
back empty". It happened twice on one machine before it was diagnosed, and the
first investigation concluded the server had been killed.

``SIGUSR1`` makes a tmux server recreate its socket at the path it was started
with, so a server whose path is still free can be reconnected without losing
anything. A server whose path a newer server now holds cannot: recreating it
would cut that newer server off instead.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def socket_path() -> str:
    """Path of this user's default tmux server socket.

    Inside tmux, ``$TMUX``'s first field IS the socket. Otherwise tmux's own
    rule: ``realpath($TMUX_TMPDIR or /tmp)/tmux-<uid>/default`` (``/tmp`` is
    ``/private/tmp`` on macOS, hence the resolve).
    """
    inside = os.environ.get("TMUX", "").split(",")[0]
    if inside:
        return inside
    base = Path(os.environ.get("TMUX_TMPDIR") or "/tmp").resolve()
    return str(base / f"tmux-{os.getuid()}" / "default")


@dataclass(frozen=True)
class OrphanedServer:
    pid: int
    socket: str
    #: True when the socket path is free, so ``SIGUSR1`` reconnects it.
    #: False when another server holds the path now.
    recoverable: bool


def _server_pids(run: Callable = subprocess.run) -> list[int]:
    """PIDs of tmux SERVERS: a server daemonizes, so its parent is init."""
    out = run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == "1" and Path(parts[2]).name == "tmux":
            pids.append(int(parts[0]))
    return pids


def _listening_path(pid: int, run: Callable = subprocess.run) -> str | None:
    """The socket path *pid* bound at startup (lsof keeps it after unlink)."""
    out = run(["lsof", "-a", "-p", str(pid), "-U", "-F", "n"],
              capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("n/"):
            return line[1:].split(" type=")[0]
    return None


def _owner_pid(path: str, run: Callable = subprocess.run) -> int | None:
    """PID of the server answering on *path*, or None if nothing answers."""
    result = run(["tmux", "-S", path, "list-sessions", "-F", "#{pid}"],
                 capture_output=True, text=True, timeout=10)
    first = result.stdout.split("\n", 1)[0].strip()
    return int(first) if result.returncode == 0 and first.isdigit() else None


def find_orphaned_servers(
    server_pids: Callable[[], list[int]] = _server_pids,
    listening_path: Callable[[int], str | None] = _listening_path,
    owner_pid: Callable[[str], int | None] = _owner_pid,
    exists: Callable[[str], bool] = os.path.exists,
) -> list[OrphanedServer]:
    """tmux servers that are running but no longer reachable on their socket."""
    orphans = []
    for pid in server_pids():
        path = listening_path(pid)
        if path is None:
            continue
        if not exists(path):
            orphans.append(OrphanedServer(pid, path, recoverable=True))
        elif owner_pid(path) != pid:
            orphans.append(OrphanedServer(pid, path, recoverable=False))
    return orphans


def reconnect(server: OrphanedServer) -> None:
    """Have *server* recreate its socket. Only for a recoverable orphan."""
    if not server.recoverable:
        raise ValueError(f"{server.socket} belongs to another server now")
    os.kill(server.pid, signal.SIGUSR1)
