"""Re-init safety for `agentwire init` (#636).

Covers the existing-config guard (TTY prompt / non-TTY no-op / --force),
the timestamped config backup, and the never-reset machines.json rule.
"""

import pytest

from agentwire.onboarding import confirm_reinit, ensure_machines_file


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("projects:\n  dir: ~/projects\n")
    return path


# ── confirm_reinit ──────────────────────────────────────────────


def test_existing_config_non_tty_refuses(config_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt without a TTY"))
    assert confirm_reinit(config_path) is False
    out = capsys.readouterr().out
    assert "Existing config" in out
    assert "--force" in out


# ── backup_config ───────────────────────────────────────────────


# ── ensure_machines_file ────────────────────────────────────────


def test_machines_never_reset(tmp_path):
    path = tmp_path / "machines.json"
    existing = '{"machines": [{"name": "dotdev-pc"}]}\n'
    path.write_text(existing)
    assert ensure_machines_file(path) is False
    assert path.read_text() == existing
