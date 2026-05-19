"""Tests for ``agentwire.missions.gc``."""

from pathlib import Path

import pytest

from agentwire.missions import dispatcher, gc, github, state
from agentwire.missions.config import MissionsConfig, RepoConfig
from agentwire.missions.github import PullRequest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "missions-state"
    monkeypatch.setattr(state, "STATE_DIR", state_dir)
    monkeypatch.setattr(state, "LAST_TICK_PATH", state_dir / "last_tick.json")
    monkeypatch.setattr(state, "ROUTED_REVIEWS_PATH", state_dir / "routed_reviews.json")
    monkeypatch.setattr(state, "NOTIFIED_PRS_PATH", state_dir / "notified_prs.json")


@pytest.fixture
def projects_dir(tmp_path):
    """Build a fake projects/<repo>-worktrees/ layout that gc can scan."""
    pd = tmp_path / "projects"
    pd.mkdir()
    return pd


@pytest.fixture
def cfg(projects_dir):
    return MissionsConfig(
        repos={
            "agentwire-dev": RepoConfig(
                short="agentwire-dev",
                name="owner/agentwire-dev",
                projects_dir=projects_dir,
            ),
        },
    )


@pytest.fixture
def patch_world(monkeypatch):
    """Fake all side-effects: session list, gh, kill, worktree remove."""

    class World:
        active_sessions: list[str] = []
        prs_by_branch: dict = {}
        killed: list[str] = []
        removed: list[Path] = []
        kill_raises: bool = False
        remove_raises: bool = False
        list_pr_raises: bool = False

    w = World()
    w.active_sessions = []
    w.prs_by_branch = {}
    w.killed = []
    w.removed = []

    monkeypatch.setattr(dispatcher, "list_mission_sessions", lambda: list(w.active_sessions))

    def _get_pr_by_branch(repo, branch):
        if w.list_pr_raises:
            raise github.GitHubError("rate limit")
        return w.prs_by_branch.get((repo, branch))

    monkeypatch.setattr(github, "get_pr_by_branch", _get_pr_by_branch)

    def _kill(session):
        if w.kill_raises:
            raise RuntimeError("kill failed")
        w.killed.append(session)

    monkeypatch.setattr(gc, "kill_session", _kill)

    def _remove(path):
        if w.remove_raises:
            raise RuntimeError("worktree remove failed")
        w.removed.append(path)

    monkeypatch.setattr(gc, "remove_worktree", _remove)
    return w


def mk_pr(n=42, state="MERGED", branch="mission-195-foo-bar"):
    return PullRequest(
        number=n,
        state=state,
        head_ref=branch,
        url=f"https://github.com/o/r/pull/{n}",
        is_draft=False,
    )


def _make_worktree_dir(projects_dir: Path, repo_short: str, branch: str) -> Path:
    """Create a fake worktree directory at the canonical location."""
    p = projects_dir / f"{repo_short}-worktrees" / branch
    p.mkdir(parents=True)
    return p


# --- tests ---


class TestNoSessions:
    def test_no_op(self, cfg, patch_world):
        report = gc.gc(cfg)
        assert report.reaped == []
        assert report.skipped == []
        # gc still records its heartbeat
        assert "gc" in state.read_last_tick()


class TestReapMerged:
    def test_merged_pr_session_reaped(self, cfg, patch_world, projects_dir):
        session = "agentwire-dev/mission-195-foo-bar"
        wt = _make_worktree_dir(projects_dir, "agentwire-dev", "mission-195-foo-bar")
        patch_world.active_sessions = [session]
        patch_world.prs_by_branch = {
            ("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr(state="MERGED"),
        }
        state.update_routed_review(42, 1001)  # gc should clear this

        report = gc.gc(cfg)
        assert len(report.reaped) == 1
        r = report.reaped[0]
        assert r["session"] == session
        assert r["pr"] == 42
        assert r["pr_state"] == "MERGED"
        assert r["worktree_removed"] is True
        assert patch_world.killed == [session]
        assert patch_world.removed == [wt]
        # state cleared for the reaped PR
        assert state.last_routed_review(42) is None

    def test_closed_pr_session_reaped(self, cfg, patch_world, projects_dir):
        session = "agentwire-dev/mission-7-thing"
        _make_worktree_dir(projects_dir, "agentwire-dev", "mission-7-thing")
        patch_world.active_sessions = [session]
        patch_world.prs_by_branch = {
            ("owner/agentwire-dev", "mission-7-thing"): mk_pr(n=8, state="CLOSED"),
        }
        report = gc.gc(cfg)
        assert len(report.reaped) == 1
        assert report.reaped[0]["pr_state"] == "CLOSED"


class TestSkips:
    def test_open_pr_untouched(self, cfg, patch_world, projects_dir):
        session = "agentwire-dev/mission-195-foo-bar"
        _make_worktree_dir(projects_dir, "agentwire-dev", "mission-195-foo-bar")
        patch_world.active_sessions = [session]
        patch_world.prs_by_branch = {
            ("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr(state="OPEN"),
        }
        report = gc.gc(cfg)
        assert report.reaped == []
        assert any("PR still OPEN" in s.get("reason", "") for s in report.skipped)
        assert patch_world.killed == []

    def test_no_pr_yet_untouched(self, cfg, patch_world):
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        report = gc.gc(cfg)
        assert report.reaped == []
        assert any("no PR for branch" in s.get("reason", "") for s in report.skipped)

    def test_unknown_repo_skipped(self, cfg, patch_world):
        patch_world.active_sessions = ["ghost/mission-1-x"]
        report = gc.gc(cfg)
        assert any("repo not in config" in s.get("reason", "") for s in report.skipped)

    def test_github_error_skips(self, cfg, patch_world):
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.list_pr_raises = True
        report = gc.gc(cfg)
        assert report.reaped == []
        assert any("get_pr_by_branch" in s.get("reason", "") for s in report.skipped)

    def test_non_mission_session_ignored(self, cfg, patch_world):
        patch_world.active_sessions = ["agentwire-dev/regular-feature-branch"]
        report = gc.gc(cfg)
        assert report.reaped == []
        # No skip recorded — it's not a mission session
        assert not any(s.get("session") == "agentwire-dev/regular-feature-branch" for s in report.skipped)


class TestKillFailure:
    def test_kill_failure_skips_session(self, cfg, patch_world, projects_dir):
        session = "agentwire-dev/mission-195-foo-bar"
        _make_worktree_dir(projects_dir, "agentwire-dev", "mission-195-foo-bar")
        patch_world.active_sessions = [session]
        patch_world.prs_by_branch = {
            ("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr(state="MERGED"),
        }
        patch_world.kill_raises = True
        report = gc.gc(cfg)
        assert report.reaped == []
        assert any("kill failed" in s.get("reason", "") for s in report.skipped)
        # worktree should not be removed if kill failed
        assert patch_world.removed == []


class TestWorktreeRemoveFailure:
    def test_remove_failure_still_reaps_session(self, cfg, patch_world, projects_dir):
        session = "agentwire-dev/mission-195-foo-bar"
        _make_worktree_dir(projects_dir, "agentwire-dev", "mission-195-foo-bar")
        patch_world.active_sessions = [session]
        patch_world.prs_by_branch = {
            ("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr(state="MERGED"),
        }
        patch_world.remove_raises = True
        report = gc.gc(cfg)
        # session was killed (kill succeeded)
        assert patch_world.killed == [session]
        # but worktree removal failed; it's recorded as a skip
        assert any("worktree remove failed" in s.get("reason", "") for s in report.skipped)
        # session is not counted as reaped because we want operator attention
        # (the session is killed but disk state needs manual cleanup)
        # Behavior: we DO add a `reaped` entry but mark worktree_removed=False.
        # This makes the kill visible in the report.
        assert len(report.reaped) == 1
        assert report.reaped[0]["worktree_removed"] is False


class TestOrphanCleanup:
    def test_orphan_worktree_removed(self, cfg, patch_world, projects_dir):
        # Worktree on disk with no matching session → orphan
        orphan = _make_worktree_dir(projects_dir, "agentwire-dev", "mission-999-orphan")
        # No active sessions
        report = gc.gc(cfg)
        assert any(o["path"] == str(orphan) for o in report.orphans_removed)
        assert orphan in patch_world.removed

    def test_live_session_dir_not_orphan(self, cfg, patch_world, projects_dir):
        session = "agentwire-dev/mission-100-live"
        wt = _make_worktree_dir(projects_dir, "agentwire-dev", "mission-100-live")
        patch_world.active_sessions = [session]
        patch_world.prs_by_branch = {
            ("owner/agentwire-dev", "mission-100-live"): mk_pr(state="OPEN"),
        }
        report = gc.gc(cfg)
        # Live session's worktree must NOT be treated as orphan
        assert wt not in patch_world.removed
        assert report.orphans_removed == []

    def test_reaped_session_dir_not_double_removed(self, cfg, patch_world, projects_dir):
        """After a session reap, the same dir must not also appear as orphan.

        Stub ``remove_worktree`` records the call but doesn't actually delete
        the dir — gc must dedup via its in-memory ``handled_worktree_paths``
        set, so ``remove`` is called exactly once across reap + orphan sweep.
        """
        session = "agentwire-dev/mission-195-foo-bar"
        wt = _make_worktree_dir(projects_dir, "agentwire-dev", "mission-195-foo-bar")
        patch_world.active_sessions = [session]
        patch_world.prs_by_branch = {
            ("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr(state="MERGED"),
        }
        report = gc.gc(cfg)
        assert len(report.reaped) == 1
        assert patch_world.removed == [wt]  # exactly once
        assert report.orphans_removed == []

    def test_orphan_remove_failure_reported(self, cfg, patch_world, projects_dir):
        _make_worktree_dir(projects_dir, "agentwire-dev", "mission-999-orphan")
        patch_world.remove_raises = True
        report = gc.gc(cfg)
        assert report.orphans_removed == []
        assert any("worktree remove failed" in s.get("reason", "") for s in report.skipped)

    def test_missing_worktrees_parent_no_error(self, cfg, patch_world, projects_dir):
        # No {repo}-worktrees dir exists at all
        report = gc.gc(cfg)
        assert report.orphans_removed == []
        assert report.skipped == []
