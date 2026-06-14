"""Tests for agentwire/ssh.py — the ControlMaster multiplexing SSOT (#300)."""

import subprocess

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


def test_ssh_base_opts_has_controlmaster_flags(socket_dir):
    opts = ssh.ssh_base_opts()
    assert _opt_value(opts, "ControlMaster") == "auto"
    assert _opt_value(opts, "ControlPersist") == str(ssh.CONTROL_PERSIST_SECONDS)
    # ControlPath is per-target via ssh's %r@%h-%p tokens, under our socket dir.
    control_path = _opt_value(opts, "ControlPath")
    assert control_path.endswith("%r@%h-%p")
    assert str(socket_dir) in control_path


def test_ssh_base_opts_has_keepalives(socket_dir):
    """ServerAliveInterval evicts a wedged master instead of hanging."""
    opts = ssh.ssh_base_opts()
    assert _opt_value(opts, "ServerAliveInterval") == "60"
    assert _opt_value(opts, "ServerAliveCountMax") == "3"


def test_ssh_base_opts_is_flat_argv(socket_dir):
    """Result splices straight into an ssh argv: flat list of strings, -o paired."""
    opts = ssh.ssh_base_opts()
    assert all(isinstance(tok, str) for tok in opts)
    # Every -o must be followed by a value token (no dangling flag).
    for i, tok in enumerate(opts):
        if tok == "-o":
            assert i + 1 < len(opts)
            assert "=" in opts[i + 1]


def test_ssh_base_opts_creates_socket_dir_on_demand(socket_dir):
    assert not socket_dir.exists()
    ssh.ssh_base_opts()
    assert socket_dir.exists()
    # 0700 — control sockets are sensitive.
    assert (socket_dir.stat().st_mode & 0o777) == 0o700


def test_ensure_socket_dir_idempotent(socket_dir):
    ssh.ensure_socket_dir()
    ssh.ensure_socket_dir()  # second call must not raise
    assert socket_dir.is_dir()


def test_ensure_socket_dir_never_raises_returns_false(monkeypatch, socket_dir):
    """A read-only home must degrade gracefully — return False, don't raise."""
    def boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(type(socket_dir), "mkdir", boom)
    assert ssh.ensure_socket_dir() is False


def test_ssh_base_opts_drops_controlmaster_when_dir_unavailable(monkeypatch, socket_dir):
    """If the socket dir can't be created, omit ControlMaster so ssh still
    connects (ControlMaster=auto + missing ControlPath dir = exit 255)."""
    monkeypatch.setattr(ssh, "ensure_socket_dir", lambda: False)
    opts = ssh.ssh_base_opts()
    assert _opt_value(opts, "ControlMaster") is None
    assert _opt_value(opts, "ControlPath") is None
    # Keepalives still present — graceful fallback, not a total strip.
    assert _opt_value(opts, "ServerAliveInterval") == "60"


def test_ssh_control_exit_invokes_o_exit(socket_dir, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    ssh.ssh_control_exit("user@host")
    argv = captured["argv"]
    assert argv[0] == "ssh"
    assert "-O" in argv and "exit" in argv
    assert argv[-1] == "user@host"
    # Targets the same per-target ControlPath the opts use.
    assert any(a.startswith("ControlPath=") and a.endswith("%r@%h-%p") for a in argv)


def test_ssh_control_exit_never_raises(socket_dir, monkeypatch):
    def boom(*a, **k):
        raise OSError("no ssh binary")

    monkeypatch.setattr(ssh.subprocess, "run", boom)
    ssh.ssh_control_exit("user@host")  # swallowed
