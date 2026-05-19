"""Tests for ``agentwire.missions.feedback_router``."""

from pathlib import Path

import pytest

from agentwire.missions import dispatcher, feedback_router, github, state
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
def cfg():
    return MissionsConfig(
        repos={
            "agentwire-dev": RepoConfig(
                short="agentwire-dev",
                name="owner/agentwire-dev",
                projects_dir=Path("/tmp"),
            ),
        },
    )


@pytest.fixture
def patch_world(monkeypatch):
    class World:
        active_sessions: list[str] = []
        pr_by_branch: dict = {}
        reviews_by_pr: dict = {}
        issue_by_n: dict = {}
        prompts_sent: list = []
        notifications_sent: list = []
        notify_result: tuple[bool, str] = (True, "stub-message-id")

    world = World()
    world.active_sessions = []
    world.pr_by_branch = {}
    world.reviews_by_pr = {}
    world.issue_by_n = {}
    world.prompts_sent = []
    world.notifications_sent = []
    world.notify_result = (True, "stub-message-id")

    monkeypatch.setattr(dispatcher, "list_mission_sessions", lambda: list(world.active_sessions))
    monkeypatch.setattr(github, "get_pr_by_branch", lambda r, b: world.pr_by_branch.get((r, b)))
    monkeypatch.setattr(
        github, "list_pr_reviews", lambda r, n: list(world.reviews_by_pr.get((r, n), []))
    )
    monkeypatch.setattr(github, "get_issue", lambda r, n: world.issue_by_n[(r, n)])
    monkeypatch.setattr(
        dispatcher,
        "send_prompt_to_worker",
        lambda s, p: world.prompts_sent.append((s, p)),
    )
    monkeypatch.setattr(feedback_router.time, "sleep", lambda s: None)

    def _stub_notify(repo_name, issue, pr):
        world.notifications_sent.append({"repo": repo_name, "issue": issue.number, "pr": pr.number})
        return world.notify_result

    monkeypatch.setattr(feedback_router, "_notify_pr_ready", _stub_notify)
    return world


# --- helpers ---


def mk_issue(n=195, title="Foo bar"):
    return Issue(
        number=n,
        title=title,
        body="## Acceptance criteria\n- a\n- b\n",
        labels=("agent-ready",),
        state="OPEN",
    )


def mk_pr(n=42, branch="mission-195-foo-bar", state="OPEN"):
    return PullRequest(
        number=n,
        state=state,
        head_ref=branch,
        url=f"https://github.com/o/r/pull/{n}",
        is_draft=True,
    )


def mk_review(rid, state="CHANGES_REQUESTED", body="please fix x", user="rev"):
    return Review(
        id=rid,
        state=state,
        body=body,
        submitted_at="2026-05-19T12:00:00Z",
        user=user,
    )


# --- tests ---


class TestNoActiveSessions:
    def test_no_op(self, cfg, patch_world):
        report = feedback_router.route_feedback(cfg)
        assert report.routed == []
        assert report.skipped == []


class TestSkips:
    def test_no_pr_yet(self, cfg, patch_world):
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        report = feedback_router.route_feedback(cfg)
        assert report.routed == []
        assert any("no PR" in s.get("reason", "") for s in report.skipped)
        assert patch_world.prompts_sent == []

    def test_pr_not_open(self, cfg, patch_world):
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.pr_by_branch = {
            ("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr(state="MERGED"),
        }
        report = feedback_router.route_feedback(cfg)
        assert any("MERGED" in s.get("reason", "") for s in report.skipped)

    def test_unknown_repo(self, cfg, patch_world):
        patch_world.active_sessions = ["ghost/mission-1-x"]
        report = feedback_router.route_feedback(cfg)
        assert any("repo not in config" in s.get("reason", "") for s in report.skipped)

    def test_no_new_reviews(self, cfg, patch_world):
        # PR opened previously and already notified → router skips quietly.
        state.mark_pr_notified(42)
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.pr_by_branch = {("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr()}
        patch_world.reviews_by_pr = {("owner/agentwire-dev", 42): []}
        patch_world.issue_by_n = {("owner/agentwire-dev", 195): mk_issue()}
        report = feedback_router.route_feedback(cfg)
        assert report.routed == []
        assert patch_world.notifications_sent == []
        assert any("no new reviews" in s.get("reason", "") for s in report.skipped)

    def test_non_mission_session_ignored(self, cfg, patch_world):
        patch_world.active_sessions = ["agentwire-dev/just-a-name"]
        report = feedback_router.route_feedback(cfg)
        assert report.routed == [] and report.skipped == []


class TestRouting:
    def test_routes_single_new_review(self, cfg, patch_world):
        state.mark_pr_notified(42)  # pretend PR already announced
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.pr_by_branch = {("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr()}
        patch_world.reviews_by_pr = {("owner/agentwire-dev", 42): [mk_review(1001)]}
        patch_world.issue_by_n = {("owner/agentwire-dev", 195): mk_issue()}

        report = feedback_router.route_feedback(cfg)
        assert len(report.routed) == 1
        # Both /clear and the refresh prompt
        assert len(patch_world.prompts_sent) == 2
        assert patch_world.prompts_sent[0] == ("agentwire-dev/mission-195-foo-bar", "/clear")
        assert "summary" in patch_world.prompts_sent[1][1].lower() or \
               "address" in patch_world.prompts_sent[1][1].lower()
        # Summary file written
        summary = feedback_router.summary_path("agentwire-dev", 195, "foo-bar")
        assert summary.exists()
        text = summary.read_text()
        assert "Mission #195" in text
        assert "please fix x" in text
        # State bumped
        assert state.last_routed_review(42) == 1001

    def test_filters_already_routed(self, cfg, patch_world):
        state.mark_pr_notified(42)
        state.update_routed_review(42, 1001)
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.pr_by_branch = {("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr()}
        patch_world.reviews_by_pr = {
            ("owner/agentwire-dev", 42): [
                mk_review(1001, body="already routed"),
                mk_review(1042, body="new one"),
            ]
        }
        patch_world.issue_by_n = {("owner/agentwire-dev", 195): mk_issue()}

        report = feedback_router.route_feedback(cfg)
        assert len(report.routed) == 1
        summary = feedback_router.summary_path("agentwire-dev", 195, "foo-bar")
        text = summary.read_text()
        assert "new one" in text
        assert "already routed" not in text
        assert state.last_routed_review(42) == 1042

    def test_summary_includes_criteria(self, cfg, patch_world):
        state.mark_pr_notified(42)
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.pr_by_branch = {("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr()}
        patch_world.reviews_by_pr = {("owner/agentwire-dev", 42): [mk_review(1001)]}
        patch_world.issue_by_n = {("owner/agentwire-dev", 195): mk_issue()}
        feedback_router.route_feedback(cfg)
        text = feedback_router.summary_path("agentwire-dev", 195, "foo-bar").read_text()
        assert "- [ ] a" in text
        assert "- [ ] b" in text


class TestPrOpenedNotification:
    def test_first_seen_pr_triggers_email(self, cfg, patch_world):
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.pr_by_branch = {("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr()}
        patch_world.reviews_by_pr = {("owner/agentwire-dev", 42): []}
        patch_world.issue_by_n = {("owner/agentwire-dev", 195): mk_issue()}

        report = feedback_router.route_feedback(cfg)

        assert patch_world.notifications_sent == [
            {"repo": "owner/agentwire-dev", "issue": 195, "pr": 42}
        ]
        assert state.is_pr_notified(42)
        assert any(r.get("event") == "pr_opened_email" for r in report.routed)

    def test_second_tick_does_not_resend(self, cfg, patch_world):
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.pr_by_branch = {("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr()}
        patch_world.reviews_by_pr = {("owner/agentwire-dev", 42): []}
        patch_world.issue_by_n = {("owner/agentwire-dev", 195): mk_issue()}

        feedback_router.route_feedback(cfg)
        feedback_router.route_feedback(cfg)

        # First tick sends the email; second is a no-op.
        assert len(patch_world.notifications_sent) == 1

    def test_send_failure_still_marks_notified(self, cfg, patch_world):
        # Don't retry on every tick when SMTP / config breaks.
        patch_world.notify_result = (False, "stub failure")
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.pr_by_branch = {("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr()}
        patch_world.reviews_by_pr = {("owner/agentwire-dev", 42): []}
        patch_world.issue_by_n = {("owner/agentwire-dev", 195): mk_issue()}

        report = feedback_router.route_feedback(cfg)
        assert state.is_pr_notified(42)
        assert any(r.get("event") == "pr_opened_email_failed" for r in report.routed)


class TestIdempotentReplay:
    def test_second_run_with_no_new_reviews_is_quiet(self, cfg, patch_world):
        state.mark_pr_notified(42)  # PR-opened email already sent
        patch_world.active_sessions = ["agentwire-dev/mission-195-foo-bar"]
        patch_world.pr_by_branch = {("owner/agentwire-dev", "mission-195-foo-bar"): mk_pr()}
        patch_world.reviews_by_pr = {("owner/agentwire-dev", 42): [mk_review(1001)]}
        patch_world.issue_by_n = {("owner/agentwire-dev", 195): mk_issue()}

        feedback_router.route_feedback(cfg)
        feedback_router.route_feedback(cfg)
        # First run: /clear + refresh = 2 prompts. Second run: 0.
        assert len(patch_world.prompts_sent) == 2


class TestBuildSummary:
    def test_no_criteria_marker(self):
        issue = Issue(
            number=1, title="t", body="no criteria header", labels=(), state="OPEN",
        )
        pr = mk_pr()
        text = feedback_router.build_summary(issue, pr, [mk_review(1)])
        assert "no criteria parsed" in text

    def test_no_reviews(self):
        issue = mk_issue()
        pr = mk_pr()
        text = feedback_router.build_summary(issue, pr, [])
        assert "no reviewer comments" in text
