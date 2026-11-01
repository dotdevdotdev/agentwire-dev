"""`agentwire buddy service install|uninstall` (#1184).

The one-command path from "the buddy exists" to "the buddy is supervised". It
writes protected control plane, so the gates matter as much as the write: a
human-only host check, a damage-control command block, and the beta flag.
"""

import argparse

import pytest
import yaml

from agentwire import buddy_cli, config_cli, tasks_cli
from agentwire import config as config_mod
from agentwire.config import load_config
from agentwire.voice_layer import identity


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"services": {"custom": [{"name": "other", "command": "sleep 1"}]}}))
    monkeypatch.setattr(config_cli, "_config_path", lambda: path)
    monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
    registered = set()
    monkeypatch.setattr(identity, "is_registered", lambda n: n in registered)
    monkeypatch.setattr(identity, "register", lambda n, **_: registered.add(n))
    return path, registered


def _run(*argv):
    parser = argparse.ArgumentParser()
    buddy_cli.register_buddy_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["buddy", "service", *argv])
    return args.func(args)


def _custom(path):
    return {e["name"]: e for e in yaml.safe_load(path.read_text())["services"]["custom"]}


def test_install_registers_writes_a_supervisable_entry_and_keeps_the_rest(cfg_file, monkeypatch):
    cfg_file, registered = cfg_file
    monkeypatch.setattr(tasks_cli, "_host_context_ok", lambda: True)

    assert _run("install", "--no-start") == 0

    assert registered == {"buddy"}
    custom = _custom(cfg_file)
    assert "other" in custom
    svc = next(s for s in load_config(cfg_file).services.custom if s.name == "agentwire-buddy")
    assert svc.command == "agentwire buddy serve buddy --port 8788"
    assert svc.autostart is False  # opt-in, per the #983 ruling
    assert svc.healthcheck.kind == "tmux_session"  # never an HTTP probe of the token route
    assert buddy_cli.buddy_service_configured(load_config(cfg_file).services.custom)

    # Idempotent, and --autostart updates in place rather than duplicating.
    assert _run("install", "--no-start", "--autostart") == 0
    assert _custom(cfg_file)["agentwire-buddy"]["autostart"] is True
    assert len(_custom(cfg_file)) == 2

    assert _run("uninstall") == 0
    assert set(_custom(cfg_file)) == {"other"}
    assert not buddy_cli.buddy_service_configured(load_config(cfg_file).services.custom)


def test_install_refuses_without_a_human_at_the_host(cfg_file, monkeypatch, capsys):
    cfg_file, registered = cfg_file
    monkeypatch.setattr(tasks_cli, "_host_context_ok", lambda: False)
    before = cfg_file.read_text()

    assert _run("install", "--no-start") == 1
    assert _run("uninstall") == 1

    assert cfg_file.read_text() == before
    assert not registered
    assert "human-only" in capsys.readouterr().err


def test_install_refuses_a_name_that_could_reach_the_shell(cfg_file, monkeypatch):
    cfg_file, _ = cfg_file
    monkeypatch.setattr(tasks_cli, "_host_context_ok", lambda: True)
    before = cfg_file.read_text()

    assert _run("install", "x;touch /tmp/pwn", "--no-start") == 1
    assert cfg_file.read_text() == before


def test_doctor_names_the_install_command_until_a_service_exists(cfg_file, monkeypatch, capsys):
    from agentwire import doctor_cli

    cfg_file, _ = cfg_file
    monkeypatch.setattr(config_mod, "load_config", lambda: load_config(cfg_file))
    monkeypatch.setattr(tasks_cli, "_host_context_ok", lambda: True)

    doctor_cli._render_beta_section()
    assert "agentwire buddy service install" in capsys.readouterr().out

    _run("install", "--no-start")
    capsys.readouterr()
    doctor_cli._render_beta_section()
    assert "agentwire buddy service install" not in capsys.readouterr().out
