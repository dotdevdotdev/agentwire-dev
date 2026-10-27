"""Install-parity tests (#634): pip/uv-tool installs and Linux must not
silently lose default-tier voice shims, `agentwire dev`, or the limits
watchdog. Covers source-checkout discovery, shim interpreter resolution,
and the per-platform watchdog install backends."""

import sys
from argparse import Namespace

from agentwire import limits_cli, system_cli

# === find_source_checkout ===









# === _resolve_shim_python ===







# === agentwire dev gating ===

def test_cmd_dev_gates_on_missing_checkout(monkeypatch, capsys):
    monkeypatch.setattr(system_cli, "tmux_session_exists", lambda name: False)
    monkeypatch.setattr(system_cli, "find_source_checkout", lambda: None)
    rc = system_cli.cmd_dev(Namespace())
    assert rc == 1
    err = capsys.readouterr().err
    assert "source checkout" in err
    assert "git clone" in err
    assert "AGENTWIRE_SOURCE_DIR" in err


# === limits install platform backends ===







def test_limits_install_systemd_writes_units(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(limits_cli, "SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(limits_cli, "SERVICE_PATH", tmp_path / "w.service")
    monkeypatch.setattr(limits_cli, "TIMER_PATH", tmp_path / "w.timer")
    monkeypatch.setattr(limits_cli.shutil, "which", lambda cmd: "/usr/bin/systemctl")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(limits_cli.subprocess, "run", fake_run)
    rc = limits_cli.cmd_limits_install(Namespace(dry_run=False))
    assert rc == 0
    assert (tmp_path / "w.service").exists()
    assert "ExecStart" in (tmp_path / "w.service").read_text()
    assert (tmp_path / "w.timer").exists()
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert any("enable" in c for c in calls)




