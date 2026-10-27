"""Tests for scan_dangling_worktrees (#716) — the dangling-PR detector.

The concrete failure mode from #716: a worker session that got rooted
(--created-by '') but kept its subordinate role correctly refuses to
self-merge its own green PR — and since nothing else has been told to act on
it, the PR just dangles. This scans registered worktree entries for exactly
that shape: LIVE worker session, OPEN PR, no live recorded parent.

Deliberately a shallow "has any live recorded parent" check (recorded
creator, or the `.agentwire.yml parent:` fallback via `_display_parent` —
the same precedence prompt-routing already uses), not a full
orchestrator-role verification of that parent (out of scope — see #716's
deferred merge-authority-per-edge north star). Orchestrator-kind entries are
excluded entirely: a durable orchestrator roots by design and is itself the
reviewer/merger, so a parentless orchestrator with an open PR is healthy,
not dangling.
"""

import json
from unittest.mock import MagicMock

from agentwire.session_cli import scan_dangling_worktrees


def _rows(**overrides):
    row = {"session": "proj-feature", "branch": "feature", "project": "/repo",
           "worktree_path": "/worktrees/proj/feature", "kind": "worker"}
    row.update(overrides)
    return [row]


class TestScanDanglingWorktrees:




    def test_live_open_pr_no_parent_is_dangling(self, monkeypatch):
        import agentwire.session_cli as m
        monkeypatch.setattr(m.shutil, "which", lambda *_: "/usr/bin/gh")
        monkeypatch.setattr(m, "tmux_session_exists", lambda s: True)
        monkeypatch.setattr(
            m.subprocess, "run",
            lambda *a, **k: MagicMock(returncode=0, stdout=json.dumps(
                {"state": "OPEN", "url": "https://github.com/x/y/pull/1"})),
        )
        monkeypatch.setattr(m, "_display_parent", lambda session, path="": None)
        out = scan_dangling_worktrees(_rows())
        assert len(out) == 1
        assert out[0]["session"] == "proj-feature"
        assert out[0]["reason"] == "no recorded parent"


    def test_live_open_pr_with_live_parent_is_not_dangling(self, monkeypatch):
        import agentwire.session_cli as m
        monkeypatch.setattr(m.shutil, "which", lambda *_: "/usr/bin/gh")
        monkeypatch.setattr(m, "tmux_session_exists", lambda s: True)
        monkeypatch.setattr(
            m.subprocess, "run",
            lambda *a, **k: MagicMock(returncode=0, stdout=json.dumps({"state": "OPEN", "url": "x"})),
        )
        monkeypatch.setattr(m, "_display_parent", lambda session, path="": "live-orchestrator")
        assert scan_dangling_worktrees(_rows()) == []



