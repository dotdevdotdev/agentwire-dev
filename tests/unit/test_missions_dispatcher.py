"""Tests for ``agentwire.missions.dispatcher.tick``.

All side-effecting functions (subprocess, GitHub, tmux paste) are monkeypatched
to no-op or to return canned data. The locking module is real (fcntl-based) but
points at a tmp dir.
"""

from datetime import datetime
from pathlib import Path

import pytest

from agentwire import locking
from agentwire.missions import dispatcher, github, state
from agentwire.missions.config import MissionsConfig, RepoConfig
from agentwire.missions.github import Issue

# --- shared fixtures ----------------------------------------------------------


@pytest.fixture
def isolated_locks(tmp_path, monkeypatch):
    monkeypatch.setattr(locking, "LOCKS_DIR", tmp_path / "locks")
    (tmp_path / "locks").mkdir()
    return tmp_path / "locks"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "missions-state"
    monkeypatch.setattr(state, "STATE_DIR", state_dir)
    monkeypatch.setattr(state, "LAST_TICK_PATH", state_dir / "last_tick.json")
    monkeypatch.setattr(state, "ROUTED_REVIEWS_PATH", state_dir / "routed_reviews.json")


@pytest.fixture
def config():
    return MissionsConfig(
        repos={
            "agentwire-dev": RepoConfig(
                short="agentwire-dev",
                name="owner/agentwire-dev",
                projects_dir=Path("/tmp/projects"),
                per_repo_concurrency=1,
            ),
        },
        global_concurrency=3,
        work_hours_start=9,
        work_hours_end=18,
    )


def eligible_issue(number: int = 1, title: str = "Add a thing") -> Issue:
    return Issue(
        number=number,
        title=title,
        body="## Acceptance criteria\n- do the thing\n",
        labels=("agent-ready",),
        state="OPEN",
    )


@pytest.fixture
def patch_world(monkeypatch):
    """Patch all dispatcher side-effects to no-ops by default.

    Tests override individual behaviors by reassigning attributes on the
    returned ``World`` object, or by re-monkeypatching specific helpers.
    """

    class World:
        # active session names returned by list_mission_sessions
        active_sessions: list[str] = []
        # list of issues returned by github.list_issues, per repo full name
        issues_by_repo: dict[str, list[Issue]] = {}
        # collect each invocation
        created_sessions: list[str] = []
        prompts_sent: list[tuple[str, str]] = []
        comments_made: list[tuple[str, int, str]] = []
        ready_response: bool = True
        create_raises: bool = False

    world = World()
    world.issues_by_repo = {}
    world.active_sessions = []
    world.created_sessions = []
    world.prompts_sent = []
    world.comments_made = []

    monkeypatch.setattr(dispatcher, "list_mission_sessions", lambda: list(world.active_sessions))

    def _list_issues(repo, label="agent-ready", limit=50):
        return list(world.issues_by_repo.get(repo, []))

    monkeypatch.setattr(github, "list_issues", _list_issues)

    def _create(session):
        if world.create_raises:
            raise RuntimeError("forced create failure")
        world.created_sessions.append(session)

    monkeypatch.setattr(dispatcher, "create_worker_session", _create)
    monkeypatch.setattr(dispatcher, "wait_for_session_ready", lambda s, timeout=30.0: world.ready_response)
    monkeypatch.setattr(
        dispatcher,
        "send_prompt_to_worker",
        lambda s, p: world.prompts_sent.append((s, p)),
    )
    monkeypatch.setattr(
        github,
        "comment_issue",
        lambda r, n, b: world.comments_made.append((r, n, b)),
    )
    return world


# --- tests --------------------------------------------------------------------


class TestWorkHours:
    def test_out_of_hours_short_circuit(self, config, patch_world):
        # 8am — before start (9)
        now = datetime(2026, 5, 19, 8, 0)
        report = dispatcher.tick(config, now=now)
        assert report.out_of_hours is True
        assert report.dispatched == []
        # state still recorded
        assert "dispatcher" in state.read_last_tick()

    def test_after_hours(self, config, patch_world):
        now = datetime(2026, 5, 19, 19, 0)
        report = dispatcher.tick(config, now=now)
        assert report.out_of_hours is True

    def test_within_hours(self, config, patch_world):
        now = datetime(2026, 5, 19, 12, 0)
        report = dispatcher.tick(config, now=now)
        assert report.out_of_hours is False


class TestSingleDispatch:
    def test_eligible_issue_dispatched(self, config, patch_world, isolated_locks):
        patch_world.issues_by_repo["owner/agentwire-dev"] = [eligible_issue(195, "Foo bar")]
        report = dispatcher.tick(config, now=datetime(2026, 5, 19, 12, 0))
        assert len(report.dispatched) == 1
        d = report.dispatched[0]
        assert d["issue"] == 195
        assert d["session"] == "agentwire-dev/mission-195-foo-bar"
        assert d["branch"] == "mission-195-foo-bar"
        assert patch_world.created_sessions == ["agentwire-dev/mission-195-foo-bar"]
        assert len(patch_world.prompts_sent) == 1
        session_arg, prompt = patch_world.prompts_sent[0]
        assert session_arg == "agentwire-dev/mission-195-foo-bar"
        assert "#195" in prompt
        assert "## Acceptance criteria" in prompt
        assert "mission-195-foo-bar" in prompt
        assert len(patch_world.comments_made) == 1

    def test_ineligible_issue_skipped(self, config, patch_world, isolated_locks):
        bad = Issue(number=1, title="No criteria", body="prose only", labels=("agent-ready",), state="OPEN")
        patch_world.issues_by_repo["owner/agentwire-dev"] = [bad]
        report = dispatcher.tick(config, now=datetime(2026, 5, 19, 12, 0))
        assert report.dispatched == []
        assert any(s["issue"] == 1 and "Acceptance" in s["reason"] for s in report.skipped)

    def test_create_failure_skipped(self, config, patch_world, isolated_locks):
        patch_world.issues_by_repo["owner/agentwire-dev"] = [eligible_issue(195)]
        patch_world.create_raises = True
        report = dispatcher.tick(config, now=datetime(2026, 5, 19, 12, 0))
        assert report.dispatched == []
        assert any("create_session failed" in s.get("reason", "") for s in report.skipped)

    def test_not_ready_skipped(self, config, patch_world, isolated_locks):
        patch_world.issues_by_repo["owner/agentwire-dev"] = [eligible_issue(195)]
        patch_world.ready_response = False
        report = dispatcher.tick(config, now=datetime(2026, 5, 19, 12, 0))
        assert report.dispatched == []
        assert any("not ready" in s.get("reason", "") for s in report.skipped)
        assert patch_world.prompts_sent == []


class TestConcurrencyCaps:
    def test_per_repo_cap_blocks(self, config, patch_world, isolated_locks):
        # One active session already
        patch_world.active_sessions = ["agentwire-dev/mission-100-existing"]
        patch_world.issues_by_repo["owner/agentwire-dev"] = [eligible_issue(195)]
        report = dispatcher.tick(config, now=datetime(2026, 5, 19, 12, 0))
        assert report.dispatched == []
        assert any(s.get("reason") == "per-repo concurrency cap" for s in report.skipped)

    def test_dispatches_until_cap(self, patch_world, isolated_locks):
        # Repo cap of 2; supply 3 eligible issues; expect 2 dispatched
        cfg = MissionsConfig(
            repos={
                "agentwire-dev": RepoConfig(
                    short="agentwire-dev",
                    name="owner/agentwire-dev",
                    projects_dir=Path("/tmp"),
                    per_repo_concurrency=2,
                )
            },
            global_concurrency=5,
        )
        patch_world.issues_by_repo["owner/agentwire-dev"] = [
            eligible_issue(1, "first"),
            eligible_issue(2, "second"),
            eligible_issue(3, "third"),
        ]
        report = dispatcher.tick(cfg, now=datetime(2026, 5, 19, 12, 0))
        assert len(report.dispatched) == 2
        # Sorted by issue number ASC, so 1 and 2 win
        assert [d["issue"] for d in report.dispatched] == [1, 2]

    def test_global_cap_blocks_across_repos(self, patch_world, isolated_locks):
        cfg = MissionsConfig(
            repos={
                "a": RepoConfig("a", "o/a", Path("/tmp"), per_repo_concurrency=10),
                "b": RepoConfig("b", "o/b", Path("/tmp"), per_repo_concurrency=10),
            },
            global_concurrency=1,
        )
        patch_world.issues_by_repo["o/a"] = [eligible_issue(1)]
        patch_world.issues_by_repo["o/b"] = [eligible_issue(2)]
        report = dispatcher.tick(cfg, now=datetime(2026, 5, 19, 12, 0))
        assert len(report.dispatched) == 1


class TestLocking:
    def test_lock_held_prevents_dispatch(self, config, patch_world, isolated_locks):
        patch_world.issues_by_repo["owner/agentwire-dev"] = [eligible_issue(195)]
        with locking.session_lock("mission-195"):
            report = dispatcher.tick(config, now=datetime(2026, 5, 19, 12, 0))
        assert report.dispatched == []
        assert any("locked" in s.get("reason", "") for s in report.skipped)


class TestGithubError:
    def test_list_issues_failure_skips_repo(self, config, patch_world, isolated_locks, monkeypatch):
        def _raise(repo, **kw):
            raise github.GitHubError("rate limit slowdown")

        monkeypatch.setattr(github, "list_issues", _raise)
        report = dispatcher.tick(config, now=datetime(2026, 5, 19, 12, 0))
        assert any("github error" in s.get("reason", "") for s in report.skipped)
