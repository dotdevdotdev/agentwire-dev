"""Opt-in Claude Code sandbox (#1279): the settings a launch line carries."""

import json
import re

import pytest

from agentwire.config import load_config


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sandbox:\n  allowed_domains: [example.test]\n  allow_write: [/scratch]\n")
    monkeypatch.setattr("agentwire.config.DEFAULT_CONFIG_PATH", cfg, raising=False)
    monkeypatch.setattr("agentwire.config.load_config", lambda path=None: load_config(cfg))
    return cfg


def _settings(command: str) -> dict:
    match = re.search(r"--settings '(\{.*?\})'(?: |$)", command)
    assert match, command
    return json.loads(match.group(1))


def test_off_by_default_and_on_only_when_asked():
    from agentwire.core import build_agent_command

    assert "--settings" not in build_agent_command("bypass").command
    on = build_agent_command("bypass", sandbox=True)
    assert on.sandbox and not build_agent_command("bypass").sandbox
    assert "--dangerously-skip-permissions" in on.command  # posture untouched


def test_generated_settings_hold_the_invariants_and_the_configured_allowances():
    from agentwire.core import build_agent_command

    sb = _settings(build_agent_command("bypass", sandbox=True).command)["sandbox"]
    # The guarantees that make it a sandbox at all — none of them configurable.
    assert sb["enabled"] is True
    assert sb["failIfUnavailable"] is True
    assert sb["allowUnsandboxedCommands"] is False
    assert sb["network"]["strictAllowlist"] is True
    # Allowances come from config.yaml; the tmux socket is always present.
    assert sb["filesystem"]["allowWrite"] == ["/scratch"]
    assert sb["network"]["allowedDomains"] == ["example.test"]
    assert "/tmp/tmux-1/default" in sb["network"]["allowUnixSockets"]
    # Keys absent from config.yaml keep their defaults.
    assert sb["excludedCommands"] == ["gh *"]


def test_launch_is_recorded_so_restart_can_regenerate_it(tmp_path):
    from agentwire.core import build_agent_command, load_session_metadata, record_session_launch

    record_session_launch("s", build_agent_command("bypass", sandbox=True), tmp_path)
    assert load_session_metadata("s")["sandbox"] is True
    record_session_launch("s", build_agent_command("bypass"), tmp_path)
    assert load_session_metadata("s")["sandbox"] is False
