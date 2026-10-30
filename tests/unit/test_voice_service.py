"""`agentwire voice` — the portal voice assistant service (#1198).

What matters: the installed entry launches the assistant on Haiku/medium with
its role and nothing else's defaults; a respawn keeps the conversation; the
install is human-only; and `voice ask` delivers to the one fixed service.
"""

import argparse

import pytest
import yaml

from agentwire import config_cli, services, tasks_cli, voice_cli
from agentwire.config import CustomServiceConfig, load_config
from agentwire.safety._core import check_protected_bash_command


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"services": {"custom": [{"name": "other", "command": "sleep 1"}]}}))
    monkeypatch.setattr(config_cli, "_config_path", lambda: path)
    monkeypatch.setattr("agentwire.config.load_config", lambda *a, **k: load_config(path))
    monkeypatch.setattr(services, "_source_dir", lambda: "/tmp/src")
    monkeypatch.setattr(services, "notifications_session_name", lambda: "notif")
    monkeypatch.setattr(services, "STATE_FILE", tmp_path / "services-state.json")
    return path


@pytest.fixture
def spawned(monkeypatch):
    """Capture the CLI commands start_service would run; nothing is live."""
    calls: list[list[str]] = []
    monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
    monkeypatch.setattr(services.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr("agentwire.core.load_session_metadata", lambda n: {})
    return calls


def _run(*argv):
    parser = argparse.ArgumentParser()
    voice_cli.register_voice_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["voice", *argv])
    return args.func(args)


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _installed(path):
    return next(s for s in load_config(path).services.custom if s.name == "agentwire-voice")


def test_install_writes_an_on_demand_haiku_medium_agent_service(cfg_file, spawned, monkeypatch):
    monkeypatch.setattr(tasks_cli, "_host_context_ok", lambda: True)

    assert _run("install", "--no-start") == 0
    svc = _installed(cfg_file)
    assert (svc.command, svc.roles, svc.model, svc.effort, svc.resume, svc.autostart) == (
        None, "voice-assistant", "claude-haiku-5-5", "medium", True, False,
    )
    assert services.is_service_session("agentwire-voice")

    # Idempotent; --effort low is the owner's speed fallback, updated in place.
    assert _run("install", "--no-start", "--effort", "low") == 0
    assert _installed(cfg_file).effort == "low"
    assert len(yaml.safe_load(cfg_file.read_text())["services"]["custom"]) == 2

    assert _run("uninstall") == 0
    assert [e["name"] for e in yaml.safe_load(cfg_file.read_text())["services"]["custom"]] == ["other"]


def test_fresh_start_launches_a_root_session_with_model_effort_and_role(cfg_file, spawned, monkeypatch):
    monkeypatch.setattr(tasks_cli, "_host_context_ok", lambda: True)
    _run("install", "--no-start")

    assert services.start_service(_installed(cfg_file)) == (True, "started")
    cmd = spawned[0]
    assert "new" in cmd
    assert {f: _flag(cmd, f) for f in ("-s", "--roles", "--model", "--effort", "--created-by")} == {
        "-s": "agentwire-voice", "--roles": "voice-assistant",
        "--model": "claude-haiku-5-5", "--effort": "medium", "--created-by": "",
    }


def test_respawn_with_a_launch_record_resumes_the_conversation(cfg_file, spawned, monkeypatch):
    monkeypatch.setattr(tasks_cli, "_host_context_ok", lambda: True)
    _run("install", "--no-start")
    monkeypatch.setattr(
        "agentwire.core.load_session_metadata",
        lambda n: {"cwd_at_launch": "/tmp/src", "posture": "bypass"},
    )

    assert services.start_service(_installed(cfg_file)) == (True, "restarted")
    assert "restart" in spawned[0] and "new" not in spawned[0]

    # A service that did not opt in keeps today's fresh start.
    spawned.clear()
    services.start_service(CustomServiceConfig(name="tracker", project="/tmp/src"))
    assert "new" in spawned[0]


def test_install_refuses_without_a_human_at_the_host(cfg_file, monkeypatch, capsys):
    monkeypatch.setattr(tasks_cli, "_host_context_ok", lambda: False)
    before = cfg_file.read_text()

    assert _run("install", "--no-start") == 1
    assert _run("uninstall") == 1
    assert cfg_file.read_text() == before
    assert "human-only" in capsys.readouterr().err


def test_an_agent_bash_tool_cannot_run_the_install():
    blocked = [c for c in ("agentwire voice install",
                           "agentwire voice uninstall",
                           "cd /tmp && agentwire  voice install --autostart")
               if not check_protected_bash_command(c)[0]]
    allowed = [c for c in ('agentwire voice ask "which sessions are busy"',
                           "agentwire voice status --json",
                           "agentwire voice restart")
               if check_protected_bash_command(c)[0]]
    assert not blocked, blocked
    assert not allowed, allowed


def test_ask_starts_the_service_and_delivers_to_it(cfg_file, spawned, monkeypatch):
    monkeypatch.setattr(tasks_cli, "_host_context_ok", lambda: True)
    _run("install", "--no-start")
    services.set_disabled("agentwire-voice", True)  # a prior `voice stop`
    sent = []
    monkeypatch.setattr("agentwire.send_cli.cmd_send", lambda a: sent.append(a) or 0)

    assert _run("ask", "which", "sessions", "are", "busy") == 0
    assert "new" in spawned[0]
    assert (sent[0].session, sent[0].prompt, sent[0].wait_ready) == (
        "agentwire-voice", ["which sessions are busy"], True,
    )
    assert "agentwire-voice" not in services.load_disabled()


def test_ask_without_install_says_how_to_install(cfg_file, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr("agentwire.send_cli.cmd_send", lambda a: sent.append(a) or 0)
    assert _run("ask", "hello", "--json") == 1
    assert "agentwire voice install" in capsys.readouterr().out
    assert not sent
