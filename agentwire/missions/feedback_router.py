"""PR feedback router: inject new PR reviews into the worker session.

Stateless. Runs every 15 minutes from launchd, or on demand via
``agentwire mission route-feedback``.

For each active mission session, find the associated PR by branch, list its
reviews, route any not-yet-routed ones (state-tracked per PR) into the
worker's session as a ``/clear`` + context-refresh prompt pair pointing at a
per-mission summary file. The summary file (not Claude's in-context memory)
is the durable record of what the worker should do next — keeps the worker's
context bounded across many feedback rounds.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agentwire.missions import dispatcher, eligibility, github, naming, state
from agentwire.missions.config import MissionsConfig, load_config

log = logging.getLogger(__name__)

SUMMARIES_DIR = Path.home() / ".agentwire" / "missions" / "summaries"

# Settle delay after sending /clear before the next prompt arrives.
CLEAR_SETTLE_SECONDS = 1.0


@dataclass
class FeedbackReport:
    """Result of one feedback-router tick — useful for CLI/JSON output."""

    started_at: str
    routed: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "routed": list(self.routed),
            "skipped": list(self.skipped),
        }


def summary_path(repo_short: str, issue_number: int, slug: str) -> Path:
    """Filesystem path for a mission's per-round summary file."""
    return SUMMARIES_DIR / repo_short / f"{naming.branch_name(issue_number, slug)}.md"


def build_summary(
    issue: github.Issue,
    pr: github.PullRequest,
    new_reviews: list[github.Review],
) -> str:
    """Compose the markdown the worker reads on each refresh round."""
    criteria = eligibility.extract_acceptance_criteria(issue.body) or []
    crit_lines = "\n".join(f"- [ ] {c}" for c in criteria) or "- (no criteria parsed)"
    review_blocks = []
    for r in new_reviews:
        body = r.body.strip() if r.body else "(no body)"
        review_blocks.append(
            f"### Review by @{r.user} ({r.state}) — {r.submitted_at}\n\n{body}\n"
        )
    reviews_section = "\n".join(review_blocks) if review_blocks else "(no reviewer comments)"
    return (
        f"# Mission #{issue.number}: {issue.title}\n"
        f"\n"
        f"PR: {pr.url}\n"
        f"\n"
        f"## Acceptance criteria\n"
        f"{crit_lines}\n"
        f"\n"
        f"## New review feedback\n"
        f"\n"
        f"{reviews_section}\n"
        f"\n"
        f"## Next steps\n"
        f"\n"
        f"Address the feedback above. Commit and push. Stop and wait for the next review.\n"
    )


def _send_context_refresh(session_full: str, summary_file: Path) -> None:
    """Send ``/clear`` and then a refresh prompt pointing at ``summary_file``.

    Two paste operations with a small settle between so Claude has time to
    process the ``/clear`` before the next prompt arrives.
    """
    dispatcher.send_prompt_to_worker(session_full, "/clear")
    time.sleep(CLEAR_SETTLE_SECONDS)
    refresh = (
        f"New PR review feedback is in. Read this summary file and address "
        f"each item, then commit and push:\n\n"
        f"  {summary_file}\n\n"
        f"When done, stop and wait for the next review round."
    )
    dispatcher.send_prompt_to_worker(session_full, refresh)


def route_feedback(config: MissionsConfig | None = None) -> FeedbackReport:
    """One router tick.

    Walks active mission sessions, finds new PR reviews, writes summary files,
    and pushes ``/clear`` + refresh prompts into each affected worker.
    """
    if config is None:
        config = load_config()

    report = FeedbackReport(started_at=datetime.now().isoformat())

    for session_full in dispatcher.list_mission_sessions():
        parsed = naming.parse_mission_session(session_full)
        if not parsed:
            continue
        repo_short, issue_number, slug = parsed

        repo = config.get_repo(repo_short)
        if repo is None:
            report.skipped.append({"session": session_full, "reason": "repo not in config"})
            continue

        branch = naming.branch_name(issue_number, slug)
        try:
            pr = github.get_pr_by_branch(repo.name, branch)
        except github.GitHubError as e:
            report.skipped.append({"session": session_full, "reason": f"get_pr_by_branch: {e}"})
            continue
        if pr is None:
            report.skipped.append({"session": session_full, "reason": "no PR for branch yet"})
            continue
        if pr.state != "OPEN":
            report.skipped.append({"session": session_full, "reason": f"PR is {pr.state}"})
            continue

        try:
            reviews = github.list_pr_reviews(repo.name, pr.number)
        except github.GitHubError as e:
            report.skipped.append({"session": session_full, "reason": f"list_pr_reviews: {e}"})
            continue

        last_seen = state.last_routed_review(pr.number) or 0
        new_reviews = [r for r in reviews if r.id > last_seen]
        if not new_reviews:
            report.skipped.append({"session": session_full, "reason": "no new reviews"})
            continue

        try:
            issue = github.get_issue(repo.name, issue_number)
        except github.GitHubError as e:
            report.skipped.append({"session": session_full, "reason": f"get_issue: {e}"})
            continue

        summary = build_summary(issue, pr, new_reviews)
        summary_file = summary_path(repo_short, issue_number, slug)
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        summary_file.write_text(summary)

        try:
            _send_context_refresh(session_full, summary_file)
        except Exception as e:
            report.skipped.append({"session": session_full, "reason": f"send failed: {e}"})
            continue

        latest_id = max(r.id for r in new_reviews)
        state.update_routed_review(pr.number, latest_id)
        report.routed.append(
            {
                "session": session_full,
                "pr": pr.number,
                "reviews_routed": len(new_reviews),
                "summary": str(summary_file),
            }
        )

    state.record_tick("feedback_router")
    return report
