"""Tests for agentwire/config.py — Config loading, env overrides, merge."""

import os
from pathlib import Path

import pytest
import yaml

from agentwire.config import (
    Config,
    _parse_env_value,
    _merge_dict,
    _apply_env_overrides,
    load_config,
)


# --- _parse_env_value ---

class TestParseEnvValue:
    @pytest.mark.parametrize("input_val,expected", [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("YES", True),
        ("1", True),
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("no", False),
        ("NO", False),
        ("0", False),
        ("42", 42),
        ("-1", -1),
        ("3.14", 3.14),
        ("0.5", 0.5),
        ("hello", "hello"),
        ("", ""),
        ("/path/to/file", "/path/to/file"),
    ])
    def test_parsing(self, input_val, expected):
        result = _parse_env_value(input_val)
        assert result == expected
        assert type(result) == type(expected)


# --- _merge_dict ---

class TestMergeDict:
    def test_shallow_merge(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _merge_dict(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_deep_merge_preserves_nested(self):
        base = {"server": {"host": "0.0.0.0", "port": 8765}}
        override = {"server": {"port": 9000}}
        result = _merge_dict(base, override)
        assert result["server"]["host"] == "0.0.0.0"
        assert result["server"]["port"] == 9000

    def test_override_replaces_non_dict(self):
        base = {"key": {"nested": True}}
        override = {"key": "simple_string"}
        result = _merge_dict(base, override)
        assert result["key"] == "simple_string"

    def test_original_unchanged(self):
        base = {"a": 1}
        override = {"b": 2}
        _merge_dict(base, override)
        assert "b" not in base


# --- _apply_env_overrides ---

class TestApplyEnvOverrides:
    def test_nested_key(self, monkeypatch, clean_env):
        monkeypatch.setenv("AGENTWIRE_SERVER__PORT", "9999")
        data = {"server": {"port": 8765}}
        result = _apply_env_overrides(data)
        assert result["server"]["port"] == 9999

    def test_creates_missing_keys(self, monkeypatch, clean_env):
        monkeypatch.setenv("AGENTWIRE_NEW__SETTING", "value")
        data = {}
        result = _apply_env_overrides(data)
        assert result["new"]["setting"] == "value"

    def test_boolean_parsing(self, monkeypatch, clean_env):
        monkeypatch.setenv("AGENTWIRE_PROJECTS__WORKTREES__ENABLED", "false")
        data = {"projects": {"worktrees": {"enabled": True}}}
        result = _apply_env_overrides(data)
        assert result["projects"]["worktrees"]["enabled"] is False


# --- load_config ---

class TestLoadConfig:
    def test_missing_file_returns_defaults(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert isinstance(config, Config)
        assert config.server.port == 8765
        assert config.server.host == "0.0.0.0"

    def test_from_yaml(self, config_file):
        config = load_config(config_file)
        assert config.server.port == 8765
        assert config.tts.backend == "none"

    def test_ssl_not_enabled_without_certs(self, config_file):
        config = load_config(config_file)
        assert config.server.ssl.enabled is False

    def test_env_override_applies(self, config_file, monkeypatch, clean_env):
        monkeypatch.setenv("AGENTWIRE_SERVER__PORT", "1234")
        config = load_config(config_file)
        assert config.server.port == 1234

    def test_default_agent_command(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert "claude" in config.agent.command

    def test_session_defaults(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.session.default_role == "agentwire"
        assert config.session.inject_soul is True

    def test_session_from_yaml(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"session": {"default_role": "custom", "inject_soul": False}}))
        config = load_config(path)
        assert config.session.default_role == "custom"
        assert config.session.inject_soul is False
