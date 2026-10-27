"""Tests for agentwire/history_migrate.py — re-keying orphaned history (#871).

The two constraints that matter most here are that history is never destroyed
and that missing history is a normal outcome rather than a crash, so both get
tested directly rather than inferred from the happy path.
"""

import json

import pytest

from agentwire import core
from agentwire import history_migrate as hm
from agentwire.history import encode_project_path

#: The minimum that makes a transcript a CONVERSATION rather than a metadata
#: stub — a stub is a DEAD id, resumable by nothing (see history.holds_a_conversation).
TURN = '{"type":"user","message":{"role":"user"}}\n'


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """Point the module at a throwaway ~/.claude/projects."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(hm, "claude_projects_dir", lambda: root)
    return root


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    """A throwaway ~/.agentwire with a recorded session named "s"."""
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    (tmp_path / "sessions" / "s").mkdir(parents=True)
    (tmp_path / "sessions" / "s" / "metadata.json").write_text("{}")
    return tmp_path


def seed(projects, cwd, files=(("conv.jsonl", '{"type":"user"}\n'),)):
    d = projects / encode_project_path(str(cwd))
    d.mkdir(parents=True)
    for name, content in files:
        (d / name).write_text(content)
    return d


class TestApply:
    def test_migrates_and_verifies(self, projects):
        src = seed(projects, "/old/place", files=(("a.jsonl", "one"), ("b.jsonl", "two")))
        result = hm.apply("/old/place", "/new/place")
        assert result["status"] == hm.MIGRATED
        target = projects / "-new-place"
        assert (target / "a.jsonl").read_text() == "one"
        assert (target / "b.jsonl").read_text() == "two"
        # Source is kept: the cheapest recovery from a bad migration.
        assert result["source_retained"] is True
        assert src.exists()

    def test_failed_verification_leaves_both_sides_alone(self, projects, monkeypatch):
        seed(projects, "/old/place")
        calls = {"n": 0}
        real = hm._fingerprint

        def drifting(root):
            calls["n"] += 1
            out = real(root)
            return {**out, "phantom.jsonl": (1, "deadbeef")} if calls["n"] == 1 else out

        monkeypatch.setattr(hm, "_fingerprint", drifting)
        result = hm.apply("/old/place", "/new/place")
        assert result["status"] == hm.ERROR
        assert "nothing was changed" in result["detail"]
        assert not (projects / "-new-place").exists()
        assert (projects / "-old-place" / "conv.jsonl").exists()


class TestMixedProvenance:
    def _jsonl(self, d, name, cwd):
        (d / name).write_text(json.dumps({"type": "user", "cwd": cwd}) + "\n")

    def test_prune_source_refuses_when_transcripts_were_left_behind(self, projects):
        """Otherwise --prune-source destroys exactly what we declined to move."""
        d = projects / encode_project_path("/p/one")
        d.mkdir(parents=True)
        self._jsonl(d, "own.jsonl", "/p/one")
        self._jsonl(d, "other.jsonl", "/p/elsewhere")

        result = hm.apply("/p/one", "/p/moved", prune_source=True)
        assert result["status"] == hm.MIGRATED
        assert result["source_retained"] is True
        assert "NOT pruned" in result["detail"]
        assert (d / "other.jsonl").exists()


