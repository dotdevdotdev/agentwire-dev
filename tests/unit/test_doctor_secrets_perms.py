"""doctor must notice a world-readable secrets file (#887).

`chmod 600 ~/.agentwire/.env` was documented convention and nothing else — the
machine PR #881 was written on had it at 0644, holding every API key, and no
diagnostic anywhere said so. These tests pin the check, its counting, and its
opt-in heal.
"""

import os
import stat

import pytest

from agentwire import core, security
from agentwire.doctor_cli import (
    _overly_permissive_secret_paths,
    _render_secrets_permissions_section,
)


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """A fake ~/.agentwire with every owner-only path present and correct."""
    cfg = tmp_path / ".agentwire"
    cfg.mkdir(mode=0o700)
    (cfg / ".env").write_text("RESEND_API_KEY=sk-test\n")
    (cfg / ".env").chmod(0o600)
    (cfg / "machines.json").write_text("{}")
    (cfg / "machines.json").chmod(0o600)
    (cfg / "portal.token").write_text("tok\n")
    (cfg / "portal.token").chmod(0o600)
    monkeypatch.setattr(core, "CONFIG_DIR", cfg)
    monkeypatch.setattr(security, "TOKEN_FILE", cfg / "portal.token")
    return cfg


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class TestDetection:
    def test_clean_install_reports_nothing(self, config_dir):
        assert _overly_permissive_secret_paths() == []

    def test_world_readable_secrets_file_is_flagged(self, config_dir):
        (config_dir / ".env").chmod(0o644)
        flagged = {str(t.path): t.mode for t in _overly_permissive_secret_paths()}
        assert str(config_dir / ".env") in flagged
        assert flagged[str(config_dir / ".env")] == 0o644

    def test_group_readable_is_flagged_too(self, config_dir):
        (config_dir / ".env").chmod(0o640)
        assert [t.path for t in _overly_permissive_secret_paths()] \
            == [config_dir / ".env"]

    def test_config_dir_token_and_machines_are_covered(self, config_dir):
        config_dir.chmod(0o755)
        (config_dir / "machines.json").chmod(0o644)
        (config_dir / "portal.token").chmod(0o644)
        assert {t.path for t in _overly_permissive_secret_paths()} == {
            config_dir,
            config_dir / "machines.json",
            config_dir / "portal.token",
        }

    def test_absent_paths_are_not_flagged(self, config_dir):
        (config_dir / "machines.json").unlink()
        (config_dir / "portal.token").unlink()
        assert _overly_permissive_secret_paths() == []

    def test_tighter_than_required_is_left_alone(self, config_dir):
        """0400 is narrower, not wider — never "fix" it back open."""
        (config_dir / ".env").chmod(0o400)
        assert _overly_permissive_secret_paths() == []


class TestRendering:
    def test_counts_toward_issues_and_names_the_fix(
            self, config_dir, capsys, monkeypatch):
        monkeypatch.setattr("agentwire.doctor_cli._confirm", lambda prompt: False)
        (config_dir / ".env").chmod(0o644)
        found, fixed = _render_secrets_permissions_section()
        out = capsys.readouterr().out
        assert (found, fixed) == (1, 0)
        assert "0644" in out
        assert f"chmod 600 {config_dir / '.env'}" in out

    def test_clean_install_prints_ok_and_counts_zero(self, config_dir, capsys):
        assert _render_secrets_permissions_section() == (0, 0)
        assert "[!!]" not in capsys.readouterr().out

    def test_dry_run_reports_without_touching_the_mode(self, config_dir, capsys):
        (config_dir / ".env").chmod(0o644)
        found, fixed = _render_secrets_permissions_section(
            auto_confirm=True, dry_run=True)
        assert (found, fixed) == (1, 0)
        assert "dry-run" in capsys.readouterr().out
        assert _mode(config_dir / ".env") == 0o644

    def test_auto_confirm_tightens_every_path(self, config_dir, capsys):
        config_dir.chmod(0o755)
        (config_dir / ".env").chmod(0o644)
        (config_dir / "machines.json").chmod(0o666)
        found, fixed = _render_secrets_permissions_section(auto_confirm=True)
        assert (found, fixed) == (3, 3)
        assert _mode(config_dir) == 0o700
        assert _mode(config_dir / ".env") == 0o600
        assert _mode(config_dir / "machines.json") == 0o600
        assert _overly_permissive_secret_paths() == []

    def test_without_confirmation_nothing_is_changed(self, config_dir, monkeypatch):
        monkeypatch.setattr("agentwire.doctor_cli._confirm", lambda prompt: False)
        (config_dir / ".env").chmod(0o644)
        found, fixed = _render_secrets_permissions_section()
        assert (found, fixed) == (1, 0)
        assert _mode(config_dir / ".env") == 0o644

    def test_a_failed_chmod_is_reported_not_counted_as_fixed(
            self, config_dir, monkeypatch, capsys):
        (config_dir / ".env").chmod(0o644)

        def boom(path, mode):
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr(os, "chmod", boom)
        found, fixed = _render_secrets_permissions_section(auto_confirm=True)
        assert (found, fixed) == (1, 0)
        assert "read-only filesystem" in capsys.readouterr().out
