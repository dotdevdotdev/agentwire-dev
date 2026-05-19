"""Concurrency tests for the missions subsystem.

Verifies the per-repo and global concurrency caps, plus that the per-issue
lock prevents two concurrent dispatcher runs from racing on the same issue.
"""

from datetime import datetime

import pytest

from agentwire import locking
from agentwire.missions import dispatcher, github, state
from agentwire.missions.config import MissionsConfig, RepoConfig
from agentwire.missions.github import Issue


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    state_dir = tmp_path / "missions-state"
    monkeypatch.setattr(state, "STATE_DIR", state_dir)
    monkeypatch.setattr(state, "LAST_TICK_PATH", state_dir / "last_tick.json")
    monkeypatch.setattr(state, "ROUTED_REVIEWS_PATH", state_dir / "routed_reviews.json")
    locks_dir = tmp_path / "locks"
    locks_dir.mkdir()
    monkeypatch.setattr(locking, "LOCKS_DIR", locks_dir)
    return tmp_path


@pytest.fixture
def world(monkeypatch, isolated):
    class World:
        issues_by_repo: dict = {}
        active_sessions: list[str] = []
        created: list[str] = []

    w = World()
    w.issues_by_repo = {}
    w.active_sessions = []
    w.created = []

    monkeypatch.setattr(github, "list_issues", lambda repo, **kw: list(w.issues_by_repo.get(repo, [])))
    monkeypatch.setattr(dispatcher, "list_mission_sessions", lambda: list(w.active_sessions))

    def _create(session):
        w.created.append(session)
        w.active_sessions.append(session)
    monkeypatch.setattr(dispatcher, "create_worker_session", _create)
    monkeypatch.setattr(dispatcher, "wait_for_session_ready", lambda s, timeout=30.0: True)
    monkeypatch.setattr(dispatcher, "send_prompt_to_worker", lambda s, p: None)
    monkeypatch.setattr(github, "comment_issue", lambda r, n, b: None)
    return w


NOON = datetime(2026, 5, 19, 12, 0)


def _issue(n, title="x"):
    return Issue(
        number=n,
        title=title,
        body="## Acceptance criteria\n- thing\n",
        labels=("agent-ready",),
        state="OPEN",
    )


def test_per_repo_concurrency_limit(world, isolated):
    """Cap=2: three eligible issues → at most two dispatched per tick."""
    cfg = MissionsConfig(
        repos={
            "agentwire-dev": RepoConfig(
                short="agentwire-dev",
                name="owner/agentwire-dev",
                projects_dir=isolated / "projects",
                per_repo_concurrency=2,
            ),
        },
        global_concurrency=5,
    )
    world.issues_by_repo["owner/agentwire-dev"] = [_issue(1, "first"), _issue(2, "second"), _issue(3, "third")]
    report = dispatcher.tick(cfg, now=NOON)
    assert len(report.dispatched) == 2
    # Lowest issue numbers win
    assert [d["issue"] for d in report.dispatched] == [1, 2]
    # A SECOND tick (same eligibility, now 2 active) → 0 dispatched, hits cap
    report2 = dispatcher.tick(cfg, now=NOON)
    assert report2.dispatched == []
    assert any("per-repo concurrency cap" in s.get("reason", "") for s in report2.skipped)


def test_global_concurrency_blocks_across_repos(world, isolated):
    """global_concurrency=1 → only one mission worker total, even across repos."""
    cfg = MissionsConfig(
        repos={
            "a": RepoConfig("a", "owner/a", isolated / "projects", per_repo_concurrency=10),
            "b": RepoConfig("b", "owner/b", isolated / "projects", per_repo_concurrency=10),
        },
        global_concurrency=1,
    )
    world.issues_by_repo["owner/a"] = [_issue(1)]
    world.issues_by_repo["owner/b"] = [_issue(2)]
    report = dispatcher.tick(cfg, now=NOON)
    assert len(report.dispatched) == 1
    # Second issue blocked by global cap
    assert any(s.get("reason") == "global concurrency cap" for s in report.skipped)


def test_lock_prevents_concurrent_dispatch_of_same_issue(world, isolated):
    """If another process holds the per-issue lock, dispatch skips that issue."""
    cfg = MissionsConfig(
        repos={
            "agentwire-dev": RepoConfig(
                short="agentwire-dev",
                name="owner/agentwire-dev",
                projects_dir=isolated / "projects",
                per_repo_concurrency=5,
            ),
        },
        global_concurrency=5,
    )
    world.issues_by_repo["owner/agentwire-dev"] = [_issue(195)]
    # Hold the lock the dispatcher would try to acquire
    with locking.session_lock("mission-195"):
        report = dispatcher.tick(cfg, now=NOON)
    assert report.dispatched == []
    assert any("locked" in s.get("reason", "") for s in report.skipped)
    # No session was created
    assert world.created == []


def test_lock_releases_after_dispatch(world, isolated):
    """After a successful dispatch, the lock is released so subsequent ticks
    can lock the same issue number again (e.g., if state is cleared and the
    issue gets re-opened)."""
    cfg = MissionsConfig(
        repos={
            "agentwire-dev": RepoConfig(
                short="agentwire-dev",
                name="owner/agentwire-dev",
                projects_dir=isolated / "projects",
                per_repo_concurrency=5,
            ),
        },
        global_concurrency=5,
    )
    world.issues_by_repo["owner/agentwire-dev"] = [_issue(195)]
    dispatcher.tick(cfg, now=NOON)
    # Now we should be able to acquire the lock again (no contention)
    with locking.session_lock("mission-195"):
        pass  # If this didn't deadlock or raise, the lock was released
