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

from agentwire import core, machine_cli


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class TestWriteOwnerOnly:
    def test_file_is_0600(self, tmp_path):
        target = tmp_path / "secret.json"
        core.write_owner_only(target, "{}")
        assert _mode(target) == 0o600
        assert target.read_text() == "{}"


class TestMachinesRegistryIsOwnerOnly:
    """The registry names hosts, users and remote paths — not world-readable."""

    @pytest.fixture(autouse=True)
    def _config_dir(self, tmp_path, monkeypatch):
        cfg = tmp_path / ".agentwire"
        cfg.mkdir()
        monkeypatch.setattr(core, "CONFIG_DIR", cfg)
        monkeypatch.setattr(machine_cli, "CONFIG_DIR", cfg)
        self.machines = cfg / "machines.json"

    def test_machine_add_writes_0600(self):
        rc = machine_cli.cmd_machine_add(
            argparse.Namespace(machine_id="gpu", host="gpu.local", user="dev", projects_dir=None)
        )
        assert rc == 0
        assert _mode(self.machines) == 0o600
        assert json.loads(self.machines.read_text())["machines"][0]["id"] == "gpu"
