"""Tests for ``agentwire.missions.github`` — gh CLI wrappers."""

import json
from dataclasses import dataclass

import pytest

from agentwire.missions import github


@dataclass
class FakeResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def fake_gh(monkeypatch):
    """Capture gh invocations and return preprogrammed responses.

    Usage:
        fake_gh.responses = [FakeResult(stdout=json.dumps([...]))]
        out = github.list_issues(...)
        assert fake_gh.calls == [["gh", "issue", "list", ...]]
    """

    class Fake:
        responses: list = []
        calls: list = []

        def __call__(self, cmd, **kwargs):
            self.calls.append(cmd)
            if not self.responses:
                return FakeResult(returncode=1, stderr="no response programmed")
            return self.responses.pop(0)

    fake = Fake()
    monkeypatch.setattr(github.subprocess, "run", fake)
    return fake


class TestRunGh:
    def test_parses_json_stdout(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout='[{"number": 1}]')]
        out = github._run_gh(["issue", "list"])
        assert out == [{"number": 1}]

    def test_empty_stdout_returns_none(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout="")]
        assert github._run_gh(["foo"]) is None

    def test_non_json_returns_text_when_parse_json_false(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout="plain text")]
        out = github._run_gh(["foo"], parse_json=False)
        assert out == "plain text"

    def test_raises_on_nonzero_returncode(self, fake_gh):
        fake_gh.responses = [FakeResult(returncode=1, stderr="boom")]
        with pytest.raises(github.GitHubError, match="boom"):
            github._run_gh(["foo"])

    def test_retries_on_rate_limit(self, fake_gh, monkeypatch):
        slept = []
        monkeypatch.setattr(github.time, "sleep", lambda s: slept.append(s))
        fake_gh.responses = [
            FakeResult(returncode=1, stderr="API rate limit exceeded"),
            FakeResult(stdout='[]'),
        ]
        out = github._run_gh(["issue", "list"])
        assert out == []
        assert slept == [2]

    def test_does_not_retry_on_normal_failure(self, fake_gh, monkeypatch):
        slept = []
        monkeypatch.setattr(github.time, "sleep", lambda s: slept.append(s))
        fake_gh.responses = [FakeResult(returncode=1, stderr="not found")]
        with pytest.raises(github.GitHubError):
            github._run_gh(["foo"])
        assert slept == []

    def test_non_json_response_raises(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout="{ not json")]
        with pytest.raises(github.GitHubError, match="non-JSON"):
            github._run_gh(["foo"])


class TestListIssues:
    def test_parses_labels(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout=json.dumps([
            {
                "number": 5,
                "title": "Add a thing",
                "body": "body text",
                "labels": [{"name": "agent-ready"}, {"name": "feature:platform"}],
                "state": "open",
            },
        ]))]
        out = github.list_issues("owner/repo")
        assert len(out) == 1
        issue = out[0]
        assert issue.number == 5
        assert issue.labels == ("agent-ready", "feature:platform")
        assert issue.state == "OPEN"

    def test_passes_repo_and_label_args(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout="[]")]
        github.list_issues("owner/repo", label="feature:platform", limit=10)
        cmd = fake_gh.calls[0]
        assert "--repo" in cmd and "owner/repo" in cmd
        assert "--label" in cmd and "feature:platform" in cmd
        assert "--limit" in cmd and "10" in cmd
        assert "--state" in cmd and "open" in cmd


class TestGetIssue:
    def test_single_issue(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout=json.dumps(
            {"number": 7, "title": "T", "body": "B", "labels": [], "state": "open"}
        ))]
        issue = github.get_issue("o/r", 7)
        assert issue.number == 7 and issue.title == "T"


class TestPullRequests:
    def test_get_pr_by_branch_found(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout=json.dumps([
            {"number": 42, "state": "OPEN", "headRefName": "mission-7-x",
             "url": "https://github.com/o/r/pull/42", "isDraft": True},
        ]))]
        pr = github.get_pr_by_branch("o/r", "mission-7-x")
        assert pr is not None and pr.number == 42 and pr.is_draft

    def test_get_pr_by_branch_none(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout="[]")]
        assert github.get_pr_by_branch("o/r", "no-such-branch") is None

    def test_get_pr_returns_none_on_error(self, fake_gh):
        fake_gh.responses = [FakeResult(returncode=1, stderr="not found")]
        assert github.get_pr("o/r", 999) is None


class TestPrReviews:
    def test_parses_review_fields(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout=json.dumps([
            {
                "id": 1001,
                "state": "CHANGES_REQUESTED",
                "body": "Please fix X",
                "submitted_at": "2026-05-19T00:00:00Z",
                "user": {"login": "reviewer1"},
            },
        ]))]
        reviews = github.list_pr_reviews("o/r", 42)
        assert len(reviews) == 1
        r = reviews[0]
        assert r.id == 1001 and r.state == "CHANGES_REQUESTED"
        assert r.user == "reviewer1"

    def test_uses_api_endpoint(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout="[]")]
        github.list_pr_reviews("o/r", 42)
        cmd = fake_gh.calls[0]
        assert cmd[1] == "api"
        assert "repos/o/r/pulls/42/reviews" in cmd


class TestCommentIssue:
    def test_passes_body(self, fake_gh):
        fake_gh.responses = [FakeResult(stdout="https://github.com/o/r/issues/1#comment-...")]
        github.comment_issue("o/r", 1, "hello")
        cmd = fake_gh.calls[0]
        assert "issue" in cmd and "comment" in cmd
        assert "--body" in cmd and "hello" in cmd
