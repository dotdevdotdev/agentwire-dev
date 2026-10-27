"""Tests for `agentwire config get|set|list` (#670).

The allowlist gate lives in the CLI, not the hook: allowlisted keys
round-trip atomically with typed validation; execution-plane, unknown, and
malformed writes are refused. All writes here target a temp config path —
never the real ~/.agentwire/config.yaml.
"""

import argparse

import pytest
import yaml

from agentwire import config_cli


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point config_cli at a temp ~/.agentwire with a seeded config.yaml."""
    monkeypatch.setattr(config_cli, "CONFIG_DIR", tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "tts": {"backend": "default", "default_voice": "af_heart"},
                "channels": {"email": {"default_to": "old@example.com"}},
                "services": {"tts": {"healthcheck": "curl localhost:8100"}},
            }
        )
    )
    return tmp_path


def _args(**kw):
    return argparse.Namespace(json=False, **kw)


def _read(config_dir):
    return yaml.safe_load((config_dir / "config.yaml").read_text())


# --- set: allowlisted round-trip --------------------------------------------






def test_set_preserves_unrelated_keys(config_dir):
    config_cli.cmd_config_set(_args(key="tts.default_voice", value="af_bella"))
    data = _read(config_dir)
    assert data["server"] == {"host": "127.0.0.1", "port": 8765}
    assert data["services"]["tts"]["healthcheck"] == "curl localhost:8100"






# --- set: refusals ------------------------------------------------------------


def test_set_refuses_execution_plane_keys(config_dir, capsys):
    bad = []
    for key in ("services.tts.healthcheck", "executables.claude", "agent.command"):
        rc = config_cli.cmd_config_set(_args(key=key, value="evil"))
        out = capsys.readouterr()
        if rc == 0 or "refused" not in out.out + out.err:
            bad.append(key)
    assert not bad, bad
    # File untouched
    assert _read(config_dir)["services"]["tts"]["healthcheck"] == "curl localhost:8100"


def test_set_refuses_malformed_values(config_dir, capsys):
    before = _read(config_dir)
    bad = []
    for key, value in (
        ("channels.email.default_to", "not-an-email"),
        ("tts.backend", "shell; rm -rf /"),
        ("server.activity_threshold_seconds", "-3"),
    ):
        rc = config_cli.cmd_config_set(_args(key=key, value=value))
        out = capsys.readouterr()
        if rc == 0 or "invalid value" not in out.out + out.err:
            bad.append((key, value))
    assert not bad, bad
    assert _read(config_dir) == before


# --- get / list ---------------------------------------------------------------












# --- the doctor invariant -----------------------------------------------------




