"""Tests for ``agentwire history migrate`` argument handling and reporting."""


import pytest

from agentwire import history_cli
from agentwire import history_migrate as hm
from agentwire.history import encode_project_path


class Args:
    def __init__(self, **kw):
        self.session = kw.get("session")
        self.from_path = kw.get("from_path")
        self.to_path = kw.get("to_path")
        self.all = kw.get("all", False)
        self.apply = kw.get("apply", False)
        self.prune_source = kw.get("prune_source", False)
        self.json = kw.get("json", False)


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(hm, "claude_projects_dir", lambda: root)
    return root


def seed(projects, cwd):
    d = projects / encode_project_path(str(cwd))
    d.mkdir(parents=True)
    (d / "conv.jsonl").write_text('{"type":"user"}\n')
    return d


class TestExitCodes:

    def test_refusal_exits_nonzero(self, projects):
        seed(projects, "/old/place")
        seed(projects, "/new/place")
        rc = history_cli.cmd_history_migrate(
            Args(from_path="/old/place", to_path="/new/place", apply=True)
        )
        assert rc == 1


