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


class TestChromeTabsRegistry:
    def test_track_and_tabs_for(self):
        chrome_tabs.track("sess-a", "tab1", url="http://localhost:3000")
        tabs = chrome_tabs.tabs_for("sess-a")
        assert len(tabs) == 1
        assert tabs[0]["tab_id"] == "tab1"
        assert tabs[0]["url"] == "http://localhost:3000"
        assert tabs[0]["tracked_at"]

    def test_tabs_for_unknown_session_is_empty(self):
        assert chrome_tabs.tabs_for("nobody") == []

    def test_track_same_tab_id_is_idempotent(self):
        chrome_tabs.track("sess-a", "tab1", url="http://localhost:3000")
        chrome_tabs.track("sess-a", "tab1", url="http://localhost:4000")
        tabs = chrome_tabs.tabs_for("sess-a")
        assert len(tabs) == 1
        assert tabs[0]["url"] == "http://localhost:4000"

    def test_untrack_removes_and_reports(self):
        chrome_tabs.track("sess-a", "tab1")
        assert chrome_tabs.untrack("sess-a", "tab1") is True
        assert chrome_tabs.tabs_for("sess-a") == []

    def test_untrack_unknown_tab_returns_false(self):
        chrome_tabs.track("sess-a", "tab1")
        assert chrome_tabs.untrack("sess-a", "does-not-exist") is False
        assert len(chrome_tabs.tabs_for("sess-a")) == 1

    def test_untrack_leaves_other_tabs_in_same_session(self):
        chrome_tabs.track("sess-a", "tab1")
        chrome_tabs.track("sess-a", "tab2")
        chrome_tabs.untrack("sess-a", "tab1")
        tabs = chrome_tabs.tabs_for("sess-a")
        assert [t["tab_id"] for t in tabs] == ["tab2"]

    def test_clear_drops_and_returns_all_for_session_only(self):
        chrome_tabs.track("sess-a", "tab1")
        chrome_tabs.track("sess-a", "tab2")
        chrome_tabs.track("sess-b", "tab3")

        cleared = chrome_tabs.clear("sess-a")
        assert {t["tab_id"] for t in cleared} == {"tab1", "tab2"}
        assert chrome_tabs.tabs_for("sess-a") == []
        assert len(chrome_tabs.tabs_for("sess-b")) == 1  # untouched

    def test_clear_empty_session_is_noop(self):
        assert chrome_tabs.clear("nobody") == []

    def test_all_tabs(self):
        chrome_tabs.track("sess-a", "tab1")
        chrome_tabs.track("sess-b", "tab2")
        data = chrome_tabs.all_tabs()
        assert set(data.keys()) == {"sess-a", "sess-b"}


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

    def test_track_requires_session_and_tab_id(self):
        rc = tabs_cli.cmd_tabs_track(self._args(session=None, tab_id="tab1"))
        assert rc == 1
        rc = tabs_cli.cmd_tabs_track(self._args(session="sess-a", tab_id=None))
        assert rc == 1

    def test_list_all_and_scoped(self, capsys):
        chrome_tabs.track("sess-a", "tab1")
        chrome_tabs.track("sess-b", "tab2")

        capsys.readouterr()
        tabs_cli.cmd_tabs_list(self._args())
        import json
        all_out = json.loads(capsys.readouterr().out)
        assert set(all_out["tabs"].keys()) == {"sess-a", "sess-b"}

        tabs_cli.cmd_tabs_list(self._args(session="sess-a"))
        scoped_out = json.loads(capsys.readouterr().out)
        assert list(scoped_out["tabs"].keys()) == ["sess-a"]
