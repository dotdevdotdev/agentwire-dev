"""Owner-only files are written owner-only, by one shared implementation (#887).

`~/.agentwire/.env` drifting to 0644 exposed the wider rule: the documented
`chmod 600` was a manual step. Nothing in the codebase writes that file (it is
hand-authored, loaded via ``load_dotenv``), but ``machines.json`` IS minted by
agentwire — with a bare ``write_text`` that inherits the umask, which is where
the live 0644 came from. Every agentwire-written owner-only file now routes
through :func:`core.write_owner_only`, the one place the
fchmod-before-any-bytes-land technique lives.
"""

import argparse
import json
import stat

import pytest

from agentwire import core, machine_cli, onboarding, security


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class TestWriteOwnerOnly:
    def test_file_is_0600(self, tmp_path):
        target = tmp_path / "secret.json"
        core.write_owner_only(target, "{}")
        assert _mode(target) == 0o600
        assert target.read_text() == "{}"

    def test_a_created_parent_is_0700(self, tmp_path):
        target = tmp_path / "nested" / "secret.json"
        core.write_owner_only(target, "{}")
        assert _mode(target.parent) == 0o700

    def test_rewrite_of_a_world_readable_file_heals_the_mode(self, tmp_path):
        """os.replace swaps the inode, so the new 0600 file wins outright."""
        target = tmp_path / "secret.json"
        target.write_text("old")
        target.chmod(0o644)
        core.write_owner_only(target, "new")
        assert _mode(target) == 0o600
        assert target.read_text() == "new"

    def test_leaves_no_temp_file_behind(self, tmp_path):
        core.write_owner_only(tmp_path / "secret.json", "{}")
        assert [p.name for p in tmp_path.iterdir()] == ["secret.json"]

    def test_a_failed_write_leaves_no_debris(self, tmp_path, monkeypatch):
        target = tmp_path / "secret.json"
        target.write_text("original")
        monkeypatch.setattr(core.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("nope")))
        with pytest.raises(OSError):
            core.write_owner_only(target, "replacement")
        assert target.read_text() == "original"
        assert [p.name for p in tmp_path.iterdir()] == ["secret.json"]


class TestMachinesRegistryIsOwnerOnly:
    """The registry names hosts, users and remote paths — not world-readable."""

    @pytest.fixture(autouse=True)
    def _config_dir(self, tmp_path, monkeypatch):
        cfg = tmp_path / ".agentwire"
        cfg.mkdir()
        monkeypatch.setattr(core, "CONFIG_DIR", cfg)
        monkeypatch.setattr(machine_cli, "CONFIG_DIR", cfg)
        self.machines = cfg / "machines.json"

    def test_onboarding_mints_it_0600(self):
        assert onboarding.ensure_machines_file(self.machines) is True
        assert _mode(self.machines) == 0o600
        assert json.loads(self.machines.read_text()) == {"machines": []}

    def test_machine_add_writes_0600(self):
        rc = machine_cli.cmd_machine_add(argparse.Namespace(
            machine_id="gpu", host="gpu.local", user="dev", projects_dir=None))
        assert rc == 0
        assert _mode(self.machines) == 0o600
        assert json.loads(self.machines.read_text())["machines"][0]["id"] == "gpu"

    def test_machine_remove_rewrites_0600(self, monkeypatch):
        machine_cli.cmd_machine_add(argparse.Namespace(
            machine_id="gpu", host="gpu.local", user=None, projects_dir=None))
        self.machines.chmod(0o644)  # drifted, e.g. minted before this fix
        rc = machine_cli.cmd_machine_remove(argparse.Namespace(machine_id="gpu"))
        assert rc == 0
        assert _mode(self.machines) == 0o600
        assert json.loads(self.machines.read_text())["machines"] == []


class TestPortalTokenStillOwnerOnly:
    """The token writer keeps its guarantees after the shared extraction."""

    def test_token_file_is_0600_in_a_0700_dir(self, tmp_path, monkeypatch):
        token_file = tmp_path / ".agentwire" / "portal.token"
        monkeypatch.setattr(security, "TOKEN_FILE", token_file)
        security.write_token_file("s3cret")
        assert _mode(token_file) == 0o600
        assert _mode(token_file.parent) == 0o700
        assert token_file.read_text() == "s3cret\n"
