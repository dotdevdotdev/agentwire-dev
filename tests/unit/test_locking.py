"""Tests for agentwire/locking.py — Lock path sanitization."""

from agentwire.locking import LOCKS_DIR, _get_lock_path


class TestLockPathSanitization:
    def test_simple_session(self):
        path = _get_lock_path("myapp")
        assert path == LOCKS_DIR / "myapp.lock"

    def test_worktree_slash_replaced(self):
        path = _get_lock_path("myapp/feature")
        assert path == LOCKS_DIR / "myapp--feature.lock"

    def test_deep_worktree(self):
        path = _get_lock_path("myapp/feat/sub")
        assert path == LOCKS_DIR / "myapp--feat--sub.lock"
