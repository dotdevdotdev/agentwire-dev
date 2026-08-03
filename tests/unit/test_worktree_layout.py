"""Unit tests for the nested worktree layout (#703).

Worktrees live at ``<worktree_dir>/<project>/<name>/`` — nested per project,
mirroring ``~/projects/<project>/`` — while tmux session names stay flat
``{project}-{name}``. Covers fallback path construction (registry miss) and
the empty-project-dir sweep after the last worktree is removed.
"""

from pathlib import Path

import pytest

from agentwire import worktree_registry as reg
from agentwire.session_cli import _cleanup_empty_project_dir, _resolve_worktree_entry


@pytest.fixture
def iso_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path / "registry")


# --- _resolve_worktree_entry: fallback (registry miss) path construction ---
#
# The conventional path is only ever a LAST-RESORT GUESS, flagged
# source="convention" (#855) so a mutating caller can refuse it.

class TestResolveFallback:
    def test_short_name(self, tmp_path, iso_registry):
        project = tmp_path / "myapp"
        wt_dir = tmp_path / "worktrees"
        ref = _resolve_worktree_entry("fix-bug", project, wt_dir)
        assert ref.session == "myapp-fix-bug"
        assert ref.path == wt_dir / "myapp" / "fix-bug"
        assert ref.source == "convention"
        assert not ref.found

    def test_full_session_name_strips_project_prefix(self, tmp_path, iso_registry):
        project = tmp_path / "myapp"
        wt_dir = tmp_path / "worktrees"
        ref = _resolve_worktree_entry("myapp-fix-bug", project, wt_dir)
        assert ref.session == "myapp-fix-bug"
        assert ref.path == wt_dir / "myapp" / "fix-bug"
        assert ref.source == "convention"

    def test_unsafe_chars_sanitized(self, tmp_path, iso_registry):
        project = tmp_path / "myapp"
        wt_dir = tmp_path / "worktrees"
        ref = _resolve_worktree_entry("feat/auth v2", project, wt_dir)
        assert ref.session == "myapp-feat-auth-v2"
        assert ref.path == wt_dir / "myapp" / "feat-auth-v2"

    def test_registry_entry_wins_over_convention(self, tmp_path, iso_registry):
        project = tmp_path / "myapp"
        project.mkdir()
        wt_dir = tmp_path / "worktrees"
        custom = tmp_path / "elsewhere" / "fix-bug"
        reg.register(project, branch="fix-bug", session="myapp-fix-bug",
                     base="main", worktree_path=custom)
        ref = _resolve_worktree_entry("fix-bug", project, wt_dir)
        assert ref.session == "myapp-fix-bug"
        assert ref.path == custom
        assert ref.source == "registry"
        assert ref.found

    def test_registry_entry_carries_branch_base_and_topology(self, tmp_path, iso_registry):
        project = tmp_path / "myapp"
        project.mkdir()
        reg.register(project, branch="fix-bug", session="myapp", base="develop",
                     worktree_path=tmp_path / "elsewhere" / "fix-bug",
                     kind="worker", topology="pane")
        ref = _resolve_worktree_entry("fix-bug", project, tmp_path / "worktrees")
        assert (ref.branch, ref.base, ref.topology) == ("fix-bug", "develop", "pane")


# --- _cleanup_empty_project_dir ---

class TestCleanupEmptyProjectDir:
    def test_removes_empty_project_dir(self, tmp_path):
        wt_dir = tmp_path / "worktrees"
        wt_path = wt_dir / "myapp" / "fix-bug"
        wt_path.parent.mkdir(parents=True)  # worktree itself already gone
        _cleanup_empty_project_dir(wt_path, wt_dir)
        assert not (wt_dir / "myapp").exists()
        assert wt_dir.exists()

    def test_keeps_project_dir_with_siblings(self, tmp_path):
        wt_dir = tmp_path / "worktrees"
        (wt_dir / "myapp" / "other").mkdir(parents=True)
        _cleanup_empty_project_dir(wt_dir / "myapp" / "fix-bug", wt_dir)
        assert (wt_dir / "myapp" / "other").exists()

    def test_never_removes_worktree_root(self, tmp_path):
        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        _cleanup_empty_project_dir(wt_dir / "myapp" / "fix-bug", wt_dir)
        assert wt_dir.exists()

    def test_ignores_paths_outside_worktree_dir(self, tmp_path):
        wt_dir = tmp_path / "worktrees"
        outside = tmp_path / "elsewhere" / "fix-bug"
        outside.parent.mkdir(parents=True)
        _cleanup_empty_project_dir(outside, wt_dir)
        assert outside.parent.exists()  # untouched — not under worktree_dir

    def test_empty_path_is_noop(self, tmp_path):
        _cleanup_empty_project_dir(Path(""), tmp_path / "worktrees")
