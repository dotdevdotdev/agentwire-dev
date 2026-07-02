"""Re-init safety for `agentwire init` (#636).

Covers the existing-config guard (TTY prompt / non-TTY no-op / --force),
the timestamped config backup, and the never-reset machines.json rule.
"""

import pytest

from agentwire.onboarding import backup_config, confirm_reinit, ensure_machines_file


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("projects:\n  dir: ~/projects\n")
    return path


# ── confirm_reinit ──────────────────────────────────────────────


def test_no_existing_config_proceeds(tmp_path):
    assert confirm_reinit(tmp_path / "config.yaml") is True


def test_force_bypasses_prompt(config_path, monkeypatch):
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("must not prompt with --force")
    )
    assert confirm_reinit(config_path, force=True) is True


def test_existing_config_non_tty_refuses(config_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("must not prompt without a TTY")
    )
    assert confirm_reinit(config_path) is False
    out = capsys.readouterr().out
    assert "Existing config" in out
    assert "--force" in out


@pytest.mark.parametrize("answer,expected", [
    ("y", True),
    ("yes", True),
    ("Y", True),
    ("", False),
    ("n", False),
    ("no", False),
])
def test_existing_config_tty_prompts(config_path, monkeypatch, answer, expected):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: answer)
    assert confirm_reinit(config_path) is expected


# ── backup_config ───────────────────────────────────────────────


def test_backup_creates_timestamped_bak(config_path):
    backup = backup_config(config_path)
    assert backup is not None
    assert backup.parent == config_path.parent
    assert backup.name.startswith("config.yaml.")
    assert backup.name.endswith(".bak")
    assert backup.read_text() == config_path.read_text()
    # Original stays in place for the subsequent overwrite
    assert config_path.exists()


def test_backup_noop_when_missing(tmp_path):
    assert backup_config(tmp_path / "config.yaml") is None
    assert list(tmp_path.iterdir()) == []


# ── ensure_machines_file ────────────────────────────────────────


def test_machines_created_when_missing(tmp_path):
    path = tmp_path / "machines.json"
    assert ensure_machines_file(path) is True
    assert path.read_text() == '{"machines": []}\n'


def test_machines_never_reset(tmp_path):
    path = tmp_path / "machines.json"
    existing = '{"machines": [{"name": "dotdev-pc"}]}\n'
    path.write_text(existing)
    assert ensure_machines_file(path) is False
    assert path.read_text() == existing
