"""doctor must notice a world-readable secrets file (#887).

`chmod 600 ~/.agentwire/.env` was documented convention and nothing else — the
machine PR #881 was written on had it at 0644, holding every API key, and no
diagnostic anywhere said so. These tests pin the check, its counting, and its
opt-in heal.
"""

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

    def test_world_readable_secrets_file_is_flagged(self, config_dir):
        (config_dir / ".env").chmod(0o644)
        flagged = {str(t.path): t.mode for t in _overly_permissive_secret_paths()}
        assert str(config_dir / ".env") in flagged
        assert flagged[str(config_dir / ".env")] == 0o644






class TestRendering:



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


