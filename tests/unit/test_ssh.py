"""Tests for agentwire/ssh.py — the ControlMaster multiplexing SSOT (#300)."""

import pytest

from agentwire import ssh


@pytest.fixture
def socket_dir(tmp_path, monkeypatch):
    """Point the socket dir at a throwaway path so tests never touch ~/.ssh."""
    d = tmp_path / "sockets"
    monkeypatch.setattr(ssh, "SSH_SOCKET_DIR", d)
    return d


def _opt_value(opts, key):
    """Pull the value of a `-o key=value` flag out of a flat opts list."""
    for i, tok in enumerate(opts):
        if tok == "-o" and opts[i + 1].startswith(f"{key}="):
            return opts[i + 1].split("=", 1)[1]
    return None


def test_ssh_base_opts_drops_controlmaster_when_dir_unavailable(monkeypatch, socket_dir):
    """If the socket dir can't be created, omit ControlMaster so ssh still
    connects (ControlMaster=auto + missing ControlPath dir = exit 255)."""
    monkeypatch.setattr(ssh, "ensure_socket_dir", lambda: False)
    opts = ssh.ssh_base_opts()
    assert _opt_value(opts, "ControlMaster") is None
    assert _opt_value(opts, "ControlPath") is None
    # Keepalives still present — graceful fallback, not a total strip.
    assert _opt_value(opts, "ServerAliveInterval") == "60"
