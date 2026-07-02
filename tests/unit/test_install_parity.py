"""Install-parity tests (#634): pip/uv-tool installs and Linux must not
silently lose default-tier voice shims, `agentwire dev`, or the limits
watchdog. Covers source-checkout discovery, shim interpreter resolution,
and the per-platform watchdog install backends."""

import sys
from argparse import Namespace

from agentwire import core, limits_cli, system_cli, tts_cli

# === find_source_checkout ===

def test_find_source_checkout_env_var(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='agentwire-dev'\n")
    monkeypatch.setenv("AGENTWIRE_SOURCE_DIR", str(tmp_path))
    assert core.find_source_checkout() == tmp_path


def test_find_source_checkout_searches_conventional_dirs(tmp_path, monkeypatch):
    checkout = tmp_path / "src" / "agentwire-dev"
    checkout.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("")
    monkeypatch.delenv("AGENTWIRE_SOURCE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(core, "load_config", lambda: {})
    assert core.find_source_checkout() == checkout


def test_find_source_checkout_none_on_package_only_install(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTWIRE_SOURCE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(core, "load_config", lambda: {})
    assert core.find_source_checkout() is None


def test_get_source_dir_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTWIRE_SOURCE_DIR", str(tmp_path))
    assert core.get_source_dir() == tmp_path


# === _resolve_shim_python ===

def test_resolve_shim_python_prefers_checkout_venv(tmp_path, monkeypatch):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    monkeypatch.setattr(core, "find_source_checkout", lambda: tmp_path)
    python, cwd, error = tts_cli._resolve_shim_python()
    assert error is None
    assert python == str(venv_python)
    assert cwd == str(tmp_path)


def test_resolve_shim_python_falls_back_to_installed_interpreter(monkeypatch):
    monkeypatch.setattr(core, "find_source_checkout", lambda: None)
    python, cwd, error = tts_cli._resolve_shim_python()
    # The test venv has fastapi/uvicorn installed, so the fallback is this
    # interpreter, run from no particular checkout.
    assert error is None
    assert python == sys.executable
    assert cwd is None


def test_resolve_shim_python_clear_error_without_wrapper_deps(monkeypatch):
    monkeypatch.setattr(core, "find_source_checkout", lambda: None)
    # None in sys.modules makes `import fastapi` raise ImportError.
    monkeypatch.setitem(sys.modules, "fastapi", None)
    python, cwd, error = tts_cli._resolve_shim_python()
    assert python is None
    assert "agentwire-dev[stt]" in error


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

def test_limits_install_dry_run_macos(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    rc = limits_cli.cmd_limits_install(Namespace(dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert limits_cli.LAUNCHD_LABEL in out
    assert "<key>StartInterval</key>" in out


def test_limits_install_dry_run_linux(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    rc = limits_cli.cmd_limits_install(Namespace(dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "limits tick" in out
    assert f"OnUnitActiveSec={limits_cli.TICK_INTERVAL}" in out
    assert "WantedBy=timers.target" in out
    assert "Type=oneshot" in out


def test_limits_install_unsupported_platform(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    rc = limits_cli.cmd_limits_install(Namespace(dry_run=True))
    assert rc == 1
    assert "no scheduler backend" in capsys.readouterr().err


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


def test_limits_uninstall_systemd(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    service = tmp_path / "w.service"
    timer = tmp_path / "w.timer"
    service.write_text("x")
    timer.write_text("x")
    monkeypatch.setattr(limits_cli, "SERVICE_PATH", service)
    monkeypatch.setattr(limits_cli, "TIMER_PATH", timer)
    monkeypatch.setattr(limits_cli.shutil, "which", lambda cmd: "/usr/bin/systemctl")
    monkeypatch.setattr(
        limits_cli.subprocess, "run",
        lambda cmd, **kw: type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    rc = limits_cli.cmd_limits_uninstall(Namespace())
    assert rc == 0
    assert not service.exists() and not timer.exists()


def test_limits_uninstall_systemd_not_installed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(limits_cli, "SERVICE_PATH", tmp_path / "missing.service")
    monkeypatch.setattr(limits_cli, "TIMER_PATH", tmp_path / "missing.timer")
    rc = limits_cli.cmd_limits_uninstall(Namespace())
    assert rc == 0
    assert "not installed" in capsys.readouterr().out
