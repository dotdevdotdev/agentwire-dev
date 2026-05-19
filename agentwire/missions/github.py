"""GitHub access for missions — thin wrappers around the ``gh`` CLI.

Why ``gh`` (and not the REST API directly): the user already has ``gh``
authenticated; building our own token plumbing duplicates that. ``gh ... --json``
covers most of what we need; PR reviews are the one gap (no ``--json`` flag on
``gh pr view``'s reviews field), so we fall through to ``gh api`` for that.

All callers receive frozen dataclasses, never raw ``gh`` JSON, so swap-outs
later (graphql, direct REST) stay invisible to ``eligibility`` / ``dispatcher``
/ ``feedback_router``.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass


class GitHubError(RuntimeError):
    """Raised when a ``gh`` call fails after retries."""


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    state: str  # "OPEN" | "CLOSED"


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str  # "OPEN" | "MERGED" | "CLOSED"
    head_ref: str
    url: str
    is_draft: bool


@dataclass(frozen=True)
class Review:
    id: int
    state: str  # "APPROVED" | "CHANGES_REQUESTED" | "COMMENTED" | "DISMISSED" | "PENDING"
    body: str
    submitted_at: str  # ISO-8601 or ""
    user: str


_RETRYABLE_STDERR_FRAGMENTS = ("rate limit", "secondary rate limit", "abuse detection")


def _run_gh(args: list[str], *, parse_json: bool = True, timeout: int = 30) -> object:
    """Run ``gh <args>``. Parses stdout as JSON unless ``parse_json`` is False.

    Retries up to 3 times on rate-limit hints (sleeping 2s between attempts).
    """
    last_err = ""
    for attempt in range(3):
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            if not parse_json:
                return result.stdout
            stdout = result.stdout.strip()
            if not stdout:
                return None
            try:
                return json.loads(stdout)
            except json.JSONDecodeError as e:
                raise GitHubError(
                    f"gh returned non-JSON for `gh {' '.join(args)}`: {e}\n{stdout[:200]}"
                ) from e

        stderr = (result.stderr or "").strip()
        last_err = stderr or result.stdout.strip()
        if attempt < 2 and any(frag in stderr.lower() for frag in _RETRYABLE_STDERR_FRAGMENTS):
            time.sleep(2)
            continue
        break
    raise GitHubError(f"gh {' '.join(args)} failed: {last_err}")


def _parse_issue(d: dict) -> Issue:
    labels = tuple(lbl.get("name", "") for lbl in (d.get("labels") or []))
    return Issue(
        number=int(d["number"]),
        title=d.get("title") or "",
        body=d.get("body") or "",
        labels=labels,
        state=(d.get("state") or "").upper(),
    )


def _parse_pr(d: dict) -> PullRequest:
    return PullRequest(
        number=int(d["number"]),
        state=(d.get("state") or "").upper(),
        head_ref=d.get("headRefName") or "",
        url=d.get("url") or "",
        is_draft=bool(d.get("isDraft", False)),
    )


def _parse_review(d: dict) -> Review:
    user = (d.get("user") or {}).get("login") or ""
    return Review(
        id=int(d["id"]),
        state=(d.get("state") or "").upper(),
        body=d.get("body") or "",
        submitted_at=d.get("submitted_at") or "",
        user=user,
    )


def list_issues(repo: str, label: str = "agent-ready", limit: int = 50) -> list[Issue]:
    """List open issues on ``repo`` carrying ``label``."""
    data = _run_gh(
        [
            "issue", "list",
            "--repo", repo,
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,labels,state",
            "--limit", str(limit),
        ]
    )
    return [_parse_issue(item) for item in (data or [])]


def get_issue(repo: str, number: int) -> Issue:
    """Fetch a single issue by number."""
    data = _run_gh(
        [
            "issue", "view", str(number),
            "--repo", repo,
            "--json", "number,title,body,labels,state",
        ]
    )
    return _parse_issue(data)


def get_pr_by_branch(repo: str, branch: str) -> PullRequest | None:
    """Return the PR whose head branch is ``branch`` (any state), or None."""
    data = _run_gh(
        [
            "pr", "list",
            "--repo", repo,
            "--head", branch,
            "--state", "all",
            "--json", "number,state,headRefName,url,isDraft",
            "--limit", "1",
        ]
    )
    if not data:
        return None
    return _parse_pr(data[0])


def get_pr(repo: str, number: int) -> PullRequest | None:
    """Fetch a single PR by number, or None if not found."""
    try:
        data = _run_gh(
            [
                "pr", "view", str(number),
                "--repo", repo,
                "--json", "number,state,headRefName,url,isDraft",
            ]
        )
    except GitHubError:
        return None
    if not data:
        return None
    return _parse_pr(data)


def list_pr_reviews(repo: str, pr_number: int) -> list[Review]:
    """List review submissions on a PR.

    Uses ``gh api repos/{repo}/pulls/{n}/reviews`` since ``gh pr view`` has no
    ``--json reviews`` flag.
    """
    data = _run_gh(["api", f"repos/{repo}/pulls/{pr_number}/reviews"])
    if not isinstance(data, list):
        return []
    return [_parse_review(item) for item in data]


def comment_issue(repo: str, number: int, body: str) -> None:
    """Post a comment on an issue."""
    _run_gh(
        [
            "issue", "comment", str(number),
            "--repo", repo,
            "--body", body,
        ],
        parse_json=False,
    )
