"""Shared test fixtures for the AgentWire test suite."""

import os
from pathlib import Path

import pytest
import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_real_outbound_email(request, monkeypatch):
    """No test may send real email — ever.

    ``_escalate_dead_letter`` (and friends) call the live Resend wiring, so a
    test that dead-letters a done/request/escalation message without mocking
    ``send_email`` silently emails the owner on every suite run (found the hard
    way: ``test_purge_leaves_ingest_and_dead`` flooded the inbox with
    "undelivered done: x → s" from its fixture names). Tests that assert on
    email re-patch the same target via ``monkeypatch``, which overrides this.

    ``test_channels.py`` is exempt: it tests ``send_email`` itself (against a
    mocked Resend transport), so stubbing the function would test the stub.
    """
    if request.node.fspath.basename == "test_channels.py":
        return
    from types import SimpleNamespace

    monkeypatch.setattr(
        "agentwire.channels.email.send_email",
        lambda **kw: SimpleNamespace(success=True, id="test-stub"),
    )


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
        "tts": {"backend": "default"},
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


@pytest.fixture(autouse=True)
def isolated_device_registry(tmp_path, monkeypatch):
    """Point the device registry + pairings at a temp dir for every test.

    Keeps the suite from reading/writing the developer's real
    ~/.agentwire/devices.json, and clears the mtime cache between tests.
    """
    from agentwire import devices

    monkeypatch.setattr(devices, "DEVICES_FILE", tmp_path / "aw-devices.json")
    monkeypatch.setattr(devices, "PAIRINGS_FILE", tmp_path / "aw-pairings.json")
    devices._cache.clear()
    yield
    devices._cache.clear()


@pytest.fixture
def clean_env(monkeypatch):
    """Remove all AGENTWIRE_* env vars."""
    for key in list(os.environ):
        if key.startswith("AGENTWIRE_"):
            monkeypatch.delenv(key)
