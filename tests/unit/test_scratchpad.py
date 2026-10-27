"""Tests for agentwire/scratchpad.py — shared notes storage (#156 redesign)."""

import json

import pytest

from agentwire import scratchpad


@pytest.fixture(autouse=True)
def pad_file(tmp_path, monkeypatch):
    f = tmp_path / "scratchpad.json"
    monkeypatch.setattr(scratchpad, "SCRATCHPAD_FILE", f)
    return f


class TestCLI:
    @pytest.fixture(autouse=True)
    def no_portal_ping(self, monkeypatch):
        from agentwire import system_cli as sys_mod

        monkeypatch.setattr(sys_mod, "_ping_scratchpad_changed", lambda: None)

    def _args(self, **kw):
        import argparse

        defaults = {"json": True}
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_add_then_list(self, capsys):
        from agentwire.system_cli import cmd_scratchpad_add, cmd_scratchpad_list

        assert cmd_scratchpad_add(self._args(text="hello pad", source="test")) == 0
        capsys.readouterr()
        assert cmd_scratchpad_list(self._args()) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["notes"][0]["text"] == "hello pad"
