"""Unit tests for the nested worktree layout (#703).

Worktrees live at ``<worktree_dir>/<project>/<name>/`` — nested per project,
mirroring ``~/projects/<project>/`` — while tmux session names stay flat
``{project}-{name}``. Covers fallback path construction (registry miss) and
the empty-project-dir sweep after the last worktree is removed.
"""


import pytest

from agentwire import worktree_registry as reg
from agentwire.session_cli import _resolve_worktree_entry


@pytest.fixture
def iso_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path / "registry")


# --- _resolve_worktree_entry: fallback (registry miss) path construction ---
#
# The conventional path is only ever a LAST-RESORT GUESS, flagged
# source="convention" (#855) so a mutating caller can refuse it.

class TestResolveFallback:



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



# --- _cleanup_empty_project_dir ---

