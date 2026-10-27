"""Tests for agentwire/config.py — Config loading, env overrides, merge."""


import pytest
import yaml

from agentwire.config import (
    load_config,
)

# --- _parse_env_value ---



# --- _merge_dict ---



# --- _apply_env_overrides ---



# --- load_config ---

class TestLoadConfig:



    def test_env_override_applies(self, config_file, monkeypatch, clean_env):
        monkeypatch.setenv("AGENTWIRE_SERVER__PORT", "1234")
        config = load_config(config_file)
        assert config.server.port == 1234





# --- WorktreeConfig (worktree: orchestration key) ---



# --- two-tier voice backends ---

class TestVoiceBackends:

    def test_custom_tts_requires_url(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"tts": {"backend": "custom"}}))
        with pytest.raises(ValueError, match="tts.backend 'custom' requires tts.url"):
            load_config(path)





    def test_old_tts_vocabulary_raises(self, tmp_path):
        for old in ("chatterbox", "runpod", "kokoro"):
            path = tmp_path / f"config-{old}.yaml"
            path.write_text(yaml.dump({"tts": {"backend": old}}))
            with pytest.raises(ValueError, match="is not valid"):
                load_config(path)





