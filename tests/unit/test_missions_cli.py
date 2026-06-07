"""Tests for ``agentwire.missions.cli`` handlers.

Tests use argparse Namespace fakes (no shell invocation) and monkeypatch all
downstream side-effects (gh, dispatcher.list_mission_sessions, gc helpers).
"""

import argparse
import json
from pathlib import Path

import pytest

from agentwire.missions import cli, dispatcher, feedback_router, gc, github, state
from agentwire.missions.config import MissionsConfig, RepoConfig
from agentwire.missions.github import Issue, PullRequest, Review


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "missions-state"
    summaries_dir = tmp_path / "missions-summaries"
    monkeypatch.setattr(state, "STATE_DIR", state_dir)
    monkeypatch.setattr(state, "LAST_TICK_PATH", state_dir / "last_tick.json")
    monkeypatch.setattr(state, "ROUTED_REVIEWS_PATH", state_dir / "routed_reviews.json")
    monkeypatch.setattr(state, "NOTIFIED_PRS_PATH", state_dir / "notified_prs.json")
    monkeypatch.setattr(feedback_router, "SUMMARIES_DIR", summaries_dir)


@pytest.fixture
def cfg(tmp_path):
    return MissionsConfig(
        repos={
            "agentwire-dev": RepoConfig(
                short="agentwire-dev",
                name="owner/agentwire-dev",
                projects_dir=tmp_path / "projects",
                per_repo_concurrency=2,
            ),
        },
        global_concurrency=3,
    )


@pytest.fixture
def stub_world(monkeypatch, cfg):
    """Fake all CLI side-effects.

    Tests pre-set fields on the returned World to control fixture behavior.
    """

    class World:
        active_sessions: list[str] = []
        issues_by_repo: dict = {}
        issue_by_number: dict = {}
        prs_by_branch: dict = {}
        reviews_by_pr: dict = {}
        comments_made: list = []
        labels_edited: list = []
        labels_created: list = []
        labels_create_raises: bool = False
        comment_raises: bool = False
        label_edit_raises: bool = False
        # dispatcher side-effects
        created_sessions: list = []
        prompts_sent: list = []
        wait_ready_response: bool = True
        notifications_sent: list = []
        # gc side-effects
        kill_calls: list = []
        remove_calls: list = []
        kill_raises: bool = False
        remove_raises: bool = False

    w = World()
    w.active_sessions = []
    w.issues_by_repo = {}
    w.issue_by_number = {}
    w.prs_by_branch = {}
    w.reviews_by_pr = {}
    w.comments_made = []
    w.labels_edited = []
    w.labels_created = []
    w.created_sessions = []
    w.prompts_sent = []
    w.notifications_sent = []
    w.kill_calls = []
    w.remove_calls = []

    # CLI and every downstream component each import load_config — patch all so
    # the in-memory ``cfg`` fixture is honored end-to-end.
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(dispatcher, "load_config", lambda: cfg)
    monkeypatch.setattr(feedback_router, "load_config", lambda: cfg)
    monkeypatch.setattr(gc, "load_config", lambda: cfg)
    # feedback_router uses time.sleep between /clear and refresh; skip it
    monkeypatch.setattr(feedback_router.time, "sleep", lambda s: None)

    monkeypatch.setattr(dispatcher, "list_mission_sessions", lambda: list(w.active_sessions))
    monkeypatch.setattr(github, "list_issues", lambda repo, **kw: list(w.issues_by_repo.get(repo, [])))
    monkeypatch.setattr(github, "get_issue", lambda repo, n: w.issue_by_number[(repo, n)])
    monkeypatch.setattr(github, "get_pr_by_branch", lambda repo, b: w.prs_by_branch.get((repo, b)))
    monkeypatch.setattr(github, "list_pr_reviews", lambda repo, n: list(w.reviews_by_pr.get((repo, n), [])))

    def _edit(repo, n, add=None, remove=None):
        if w.label_edit_raises:
            raise github.GitHubError("api error")
        w.labels_edited.append({"repo": repo, "n": n, "add": add or [], "remove": remove or []})

    monkeypatch.setattr(github, "edit_issue_labels", _edit)

    def _comment(repo, n, body):
        if w.comment_raises:
            raise github.GitHubError("api error")
        w.comments_made.append({"repo": repo, "n": n, "body": body})

    monkeypatch.setattr(github, "comment_issue", _comment)

    def _create_label(repo, name, color="0e8a16", description=""):
        if w.labels_create_raises:
            raise github.GitHubError("api error")
        # Mimic idempotent contract: first call returns True, repeat returns False
        already = any(le["repo"] == repo and le["name"] == name for le in w.labels_created)
        w.labels_created.append({"repo": repo, "name": name, "color": color, "description": description})
        return not already

    monkeypatch.setattr(github, "create_label", _create_label)

    # dispatcher internals used by `mission spawn`
    def _new(session):
        w.created_sessions.append(session)

    monkeypatch.setattr(dispatcher, "create_worker_session", _new)
    monkeypatch.setattr(dispatcher, "wait_for_session_ready", lambda s, timeout=30.0: w.wait_ready_response)
    monkeypatch.setattr(dispatcher, "send_prompt_to_worker", lambda s, p: w.prompts_sent.append((s, p)))

    # gc helpers used by `mission kill`
    def _kill(session):
        if w.kill_raises:
            raise RuntimeError("kill failed")
        w.kill_calls.append(session)

    monkeypatch.setattr(gc, "kill_session", _kill)

    def _remove(path):
        if w.remove_raises:
            raise RuntimeError("remove failed")
        w.remove_calls.append(path)

    monkeypatch.setattr(gc, "remove_worktree", _remove)

    # Never send real PR-opened emails from tests
    def _stub_notify(repo_name, issue, pr):
        w.notifications_sent.append({"repo": repo_name, "issue": issue.number, "pr": pr.number})
        return True, "stub-message-id"

    monkeypatch.setattr(feedback_router, "_notify_pr_ready", _stub_notify)

    return w


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _make_issue(n=195, title="Foo bar", labels=("agent-ready",), state="OPEN",
                body="## Acceptance criteria\n- thing\n"):
    return Issue(number=n, title=title, body=body, labels=labels, state=state)


# --- tests ---


class TestList:
    def test_human_output_with_active_and_eligible(self, stub_world, capsys):
        stub_world.active_sessions = ["agentwire-dev/mission-100-existing"]
        stub_world.issues_by_repo = {
            "owner/agentwire-dev": [_make_issue(195, "Foo bar"), _make_issue(196, "Bar baz")]
        }
        rc = cli.cmd_mission_list(_ns(json=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "mission-100-existing" in out
        assert "#195" in out and "Foo bar" in out

    def test_json_output(self, stub_world, capsys):
        stub_world.active_sessions = ["agentwire-dev/mission-100-existing"]
        stub_world.issues_by_repo = {
            "owner/agentwire-dev": [_make_issue(195, "Foo bar")]
        }
        rc = cli.cmd_mission_list(_ns(json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "active" in data and "eligible" in data
        assert data["active"]["agentwire-dev"][0]["issue"] == 100
        assert data["eligible"]["agentwire-dev"][0]["issue"] == 195

    def test_github_error_recorded(self, stub_world, capsys, monkeypatch):
        def _boom(repo, **kw):
            raise github.GitHubError("rate limit")

        monkeypatch.setattr(github, "list_issues", _boom)
        rc = cli.cmd_mission_list(_ns(json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert any("rate limit" in e["error"] for e in data["errors"])


class TestShow:
    def test_no_active_session(self, stub_world, capsys):
        stub_world.issue_by_number[("owner/agentwire-dev", 195)] = _make_issue(195)
        rc = cli.cmd_mission_show(_ns(number=195, repo="agentwire-dev", json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["session"] is None
        assert data["pr"] is None
        assert data["acceptance_criteria"] == ["thing"]
        assert data["eligible"] is True

    def test_with_active_session_and_pr(self, stub_world, capsys):
        stub_world.issue_by_number[("owner/agentwire-dev", 195)] = _make_issue(195)
        stub_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        stub_world.prs_by_branch[("owner/agentwire-dev", "mission-195-foo-bar")] = PullRequest(
            number=42, state="OPEN", head_ref="mission-195-foo-bar",
            url="https://github.com/o/r/pull/42", is_draft=True,
        )
        rc = cli.cmd_mission_show(_ns(number=195, repo="agentwire-dev", json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["session"]["session"] == "agentwire-dev/mission-195-foo-bar"
        assert data["pr"]["number"] == 42 and data["pr"]["state"] == "OPEN"

    def test_unknown_repo(self, stub_world, capsys):
        rc = cli.cmd_mission_show(_ns(number=1, repo="ghost", json=True))
        assert rc == 2
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is False


class TestStatus:
    def test_summary(self, stub_world, capsys):
        stub_world.active_sessions = ["agentwire-dev/mission-100-x", "agentwire-dev/mission-200-y"]
        stub_world.issues_by_repo = {
            "owner/agentwire-dev": [_make_issue(195), _make_issue(196)]
        }
        rc = cli.cmd_mission_status(_ns(json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["global_active"] == 2
        assert data["repos"][0]["active"] == 2
        assert data["repos"][0]["eligible"] == 2


class TestSpawn:
    def test_force_dispatches_ineligible(self, stub_world, capsys, monkeypatch):
        # locking — bypass real flock
        from agentwire import locking
        monkeypatch.setattr(locking, "LOCKS_DIR", Path("/tmp/agentwire-cli-test-locks"))
        Path("/tmp/agentwire-cli-test-locks").mkdir(exist_ok=True)

        # Issue lacking the agent-ready label — normally ineligible
        ineligible = _make_issue(195, labels=("feature:platform",))
        stub_world.issue_by_number[("owner/agentwire-dev", 195)] = ineligible

        rc = cli.cmd_mission_spawn(_ns(number=195, repo="agentwire-dev", json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["dispatched"][0]["issue"] == 195

    def test_get_issue_failure(self, stub_world, capsys, monkeypatch):
        def _boom(repo, n):
            raise github.GitHubError("not found")

        monkeypatch.setattr(github, "get_issue", _boom)
        rc = cli.cmd_mission_spawn(_ns(number=999, repo="agentwire-dev", json=True))
        assert rc == 1


class TestStallResume:
    def test_stall(self, stub_world, capsys):
        rc = cli.cmd_mission_stall(_ns(number=195, repo="agentwire-dev",
                                       reason="needs more info", json=True))
        assert rc == 0
        assert stub_world.labels_edited == [
            {"repo": "owner/agentwire-dev", "n": 195,
             "add": ["stalled"], "remove": ["agent-ready"]}
        ]
        assert stub_world.comments_made[0]["body"].startswith("Mission stalled: needs more info")

    def test_resume(self, stub_world, capsys):
        rc = cli.cmd_mission_resume(_ns(number=195, repo="agentwire-dev", json=True))
        assert rc == 0
        assert stub_world.labels_edited == [
            {"repo": "owner/agentwire-dev", "n": 195,
             "add": ["agent-ready"], "remove": ["stalled"]}
        ]
        # resume should NOT post a comment
        assert stub_world.comments_made == []

    def test_stall_label_failure(self, stub_world, capsys):
        stub_world.label_edit_raises = True
        rc = cli.cmd_mission_stall(_ns(number=195, repo="agentwire-dev",
                                       reason="x", json=True))
        assert rc == 1


class TestKill:
    def test_kill_active_session(self, stub_world, capsys, tmp_path):
        session = "agentwire-dev/mission-195-foo-bar"
        stub_world.active_sessions = [session]
        # Make the worktree dir exist so remove_worktree gets called
        wt = tmp_path / "projects" / "agentwire-dev-worktrees" / "mission-195-foo-bar"
        wt.mkdir(parents=True)

        rc = cli.cmd_mission_kill(_ns(number=195, repo="agentwire-dev", json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["session"] == session
        assert data["worktree_removed"] is True
        assert stub_world.kill_calls == [session]
        assert stub_world.remove_calls == [wt]

    def test_kill_with_no_active_session(self, stub_world, capsys):
        rc = cli.cmd_mission_kill(_ns(number=999, repo="agentwire-dev", json=True))
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert "no active mission session" in out["error"]

    def test_kill_worktree_missing_no_error(self, stub_world, capsys):
        session = "agentwire-dev/mission-195-foo-bar"
        stub_world.active_sessions = [session]
        # No worktree on disk
        rc = cli.cmd_mission_kill(_ns(number=195, repo="agentwire-dev", json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["worktree_removed"] is False
        assert stub_world.remove_calls == []


class TestGc:
    def test_runs_gc(self, stub_world, capsys, monkeypatch):
        # gc is a separate module; call it through cli with no active sessions
        rc = cli.cmd_mission_gc(_ns(json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["reaped"] == [] and data["orphans_removed"] == []


class TestTick:
    def test_runs_dispatcher(self, stub_world, capsys, monkeypatch):
        # Out-of-hours fast path: should not raise; return tick-report shape
        from datetime import datetime
        monkeypatch.setattr(
            "agentwire.missions.dispatcher.datetime",
            type("D", (), {"now": classmethod(lambda cls: datetime(2026, 5, 19, 3, 0))}),
        )
        rc = cli.cmd_mission_tick(_ns(json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["out_of_hours"] is True


class TestRouteFeedback:
    def test_runs_router_no_sessions(self, stub_world, capsys):
        rc = cli.cmd_mission_route_feedback(_ns(json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["routed"] == [] and data["skipped"] == []

    def test_router_with_new_review(self, stub_world, capsys):
        state.mark_pr_notified(42)  # pretend PR already announced
        stub_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        stub_world.prs_by_branch[("owner/agentwire-dev", "mission-195-foo-bar")] = PullRequest(
            number=42, state="OPEN", head_ref="mission-195-foo-bar",
            url="https://github.com/o/r/pull/42", is_draft=True,
        )
        stub_world.reviews_by_pr[("owner/agentwire-dev", 42)] = [Review(
            id=1001, state="CHANGES_REQUESTED", body="fix x",
            submitted_at="2026-05-19T12:00:00Z", user="rev",
        )]
        stub_world.issue_by_number[("owner/agentwire-dev", 195)] = _make_issue(195)
        rc = cli.cmd_mission_route_feedback(_ns(json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["routed"]) == 1


class TestInit:
    def test_creates_labels_via_short_name(self, stub_world, capsys):
        rc = cli.cmd_mission_init(_ns(repo="agentwire-dev", json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert {entry["label"] for entry in data["labels"]} == {"agent-ready", "stalled"}
        assert all(entry["created"] is True for entry in data["labels"])
        created_names = {row["name"] for row in stub_world.labels_created}
        assert created_names == {"agent-ready", "stalled"}
        assert all(row["repo"] == "owner/agentwire-dev" for row in stub_world.labels_created)

    def test_creates_labels_via_owner_repo_form(self, stub_world, capsys):
        rc = cli.cmd_mission_init(_ns(repo="some-owner/some-repo", json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert {entry["label"] for entry in data["labels"]} == {"agent-ready", "stalled"}

    def test_idempotent_second_call(self, stub_world, capsys):
        cli.cmd_mission_init(_ns(repo="agentwire-dev", json=True))
        capsys.readouterr()  # drain first output
        rc = cli.cmd_mission_init(_ns(repo="agentwire-dev", json=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert all(entry["created"] is False for entry in data["labels"])

    def test_rejects_bare_short_with_no_config(self, stub_world, capsys):
        rc = cli.cmd_mission_init(_ns(repo="unknown-short-name", json=True))
        assert rc == 1
