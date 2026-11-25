"""Stranded tmux servers: running, but their socket is gone or taken."""

import os
from pathlib import Path
from types import SimpleNamespace

from agentwire import limits_cli, tmux_socket
from agentwire.tmux_socket import OrphanedServer


def test_find_orphaned_servers_classifies_each_server():
    paths = {10: "/s/healthy", 20: "/s/deleted", 30: "/s/taken", 40: None}
    owners = {"/s/healthy": 10, "/s/taken": 99}
    got = tmux_socket.find_orphaned_servers(
        server_pids=lambda: [10, 20, 30, 40],
        listening_path=paths.get,
        owner_pid=owners.get,
        exists=lambda p: p != "/s/deleted",
    )
    assert got == [
        OrphanedServer(20, "/s/deleted", recoverable=True),
        OrphanedServer(30, "/s/taken", recoverable=False),
    ]


def test_server_discovery_parses_ps_and_lsof():
    def run(cmd, **_):
        if cmd[0] == "ps":
            out = ("  101     1 /opt/homebrew/bin/tmux\n"
                   "  102   500 tmux\n"            # a client, not a server
                   "  103     1 /usr/bin/zsh\n")
        else:  # lsof: macOS prints the bare path, Linux appends type/state
            out = ("p101\nf5\nn->0x1234\nf6\n"
                   "n/tmp/tmux-501/default type=STREAM (LISTEN)\n")
        return SimpleNamespace(stdout=out, returncode=0)

    assert tmux_socket._server_pids(run) == [101]
    assert tmux_socket._listening_path(101, run) == "/tmp/tmux-501/default"


def test_socket_path_follows_tmux_rules(monkeypatch, tmp_path):
    uid = os.getuid()
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    default = tmux_socket.socket_path()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    custom = tmux_socket.socket_path()
    monkeypatch.setenv("TMUX", "/inside/sock,123,0")
    inside = tmux_socket.socket_path()

    assert default == str(Path("/tmp").resolve() / f"tmux-{uid}" / "default")
    assert custom == str(tmp_path.resolve() / f"tmux-{uid}" / "default")
    assert inside == "/inside/sock"


def test_watchdog_units_carry_tmux_tmpdir(monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", "/state/tmux")
    with_dir = (limits_cli._plist_content("/bin/aw"),
                limits_cli._systemd_service_content("/bin/aw"))
    monkeypatch.delenv("TMUX_TMPDIR")
    without = (limits_cli._plist_content("/bin/aw"),
               limits_cli._systemd_service_content("/bin/aw"))

    assert "<key>TMUX_TMPDIR</key>\n        <string>/state/tmux</string>" in with_dir[0]
    assert "Environment=TMUX_TMPDIR=/state/tmux\n" in with_dir[1]
    assert not any("TMUX_TMPDIR" in text for text in without)
