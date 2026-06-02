"""Shared test fixtures for the AgentWire test suite."""

import os
from pathlib import Path

import pytest
import yaml


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Temporary ~/.agentwire/ equivalent."""
    config_dir = tmp_path / ".agentwire"
    config_dir.mkdir()
    (config_dir / "locks").mkdir()
    (config_dir / "logs").mkdir()
    return config_dir


@pytest.fixture
def minimal_config_yaml():
    """Minimal valid config dict."""
    return {
        "server": {"host": "0.0.0.0", "port": 8765},
        "projects": {"dir": "~/projects"},
        "tts": {"backend": "none"},
    }


@pytest.fixture
def config_file(tmp_config_dir, minimal_config_yaml):
    """Write a config.yaml and return its path."""
    config_path = tmp_config_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.safe_dump(minimal_config_yaml, f)
    return config_path


@pytest.fixture
def project_dir(tmp_path):
    """Temporary project directory."""
    proj = tmp_path / "test-project"
    proj.mkdir()
    return proj


@pytest.fixture
def project_config_file(project_dir):
    """Write a .agentwire.yml and return its path."""
    config_path = project_dir / ".agentwire.yml"
    data = {
        "type": "claude-bypass",
        "roles": ["agentwire", "voice"],
        "voice": "default",
        "parent": "main",
    }
    with open(config_path, "w") as f:
        yaml.safe_dump(data, f)
    return config_path


@pytest.fixture
def scheduler_board_file(tmp_config_dir):
    """Write a scheduler.yaml with 3 test tasks, return path."""
    board_path = tmp_config_dir / "scheduler.yaml"
    import shutil
    shutil.copy(FIXTURES_DIR / "sample_scheduler.yaml", board_path)
    return board_path


@pytest.fixture
def clean_env(monkeypatch):
    """Remove all AGENTWIRE_* env vars."""
    for key in list(os.environ):
        if key.startswith("AGENTWIRE_"):
            monkeypatch.delenv(key)
