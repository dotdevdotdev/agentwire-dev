"""Tests for agentwire/chrome_tabs.py + agentwire/tabs_cli.py (#717).

Pure bookkeeping for claude-in-chrome tab ids a session opened, so a crashed
session's orphaned verification tabs can still be surfaced during worktree
teardown. agentwire never calls `tabs_close_mcp` itself.
"""

from argparse import Namespace

import pytest

from agentwire import chrome_tabs, tabs_cli


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome_tabs, "REGISTRY_FILE", tmp_path / "chrome-tabs.json")


class TestTabsCli:
    def _args(self, **kw):
        base = dict(session=None, tab_id=None, url=None, json=True)
        base.update(kw)
        return Namespace(**base)

    def test_track_untrack_round_trip(self, capsys):
        rc = tabs_cli.cmd_tabs_track(self._args(session="sess-a", tab_id="tab1", url="http://x"))
        assert rc == 0
        assert chrome_tabs.tabs_for("sess-a")[0]["tab_id"] == "tab1"

        capsys.readouterr()
        rc = tabs_cli.cmd_tabs_untrack(self._args(session="sess-a", tab_id="tab1"))
        assert rc == 0
        assert chrome_tabs.tabs_for("sess-a") == []
