"""Mission dispatcher: pick eligible GitHub issues, spawn worker sessions.

Stateless. Runs every 30 minutes from launchd during work hours, or on demand
via ``agentwire mission tick``. Uses ``session_lock`` keyed on issue number to
make concurrent runs safe.

Side-effecting helpers (subprocess calls, github calls, tmux paste) live as
module-level functions so tests can monkeypatch them. Pure logic stays in
``eligibility`` and ``naming``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime

from agentwire.locking import LockConflict, session_lock
from agentwire.missions import eligibility, github, naming, state
from agentwire.missions.config import MissionsConfig, RepoConfig, load_config
from agentwire.session_ready import wait_for_session_ready  # noqa: F401 — re-exported for tests/callers

log = logging.getLogger(__name__)


@dataclass
class DispatchReport:
    """Result of one tick — useful for CLI/JSON output."""

    started_at: str
    dispatched: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    out_of_hours: bool = False

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "out_of_hours": self.out_of_hours,
            "dispatched": list(self.dispatched),
            "skipped": list(self.skipped),
        }


# --- side-effecting helpers (monkeypatched in tests) --------------------------


def list_mission_sessions() -> list[str]:
    """Return names of running mission sessions (filtered through ``naming``)."""
    result = subprocess.run(
        ["agentwire", "list", "--sessions", "--json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        log.warning("agentwire list failed: %s", result.stderr.strip())
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    sessions = data.get("sessions", []) if isinstance(data, dict) else []
    names = [s.get("name", "") for s in sessions if isinstance(s, dict)]
    return [n for n in names if n and naming.is_mission_session(n)]


def create_worker_session(session_full_name: str) -> None:
    """Create a new mission worker session via ``agentwire new``.

    Raises ``RuntimeError`` on failure.
    """
    result = subprocess.run(
        [
            "agentwire", "new",
            "-s", session_full_name,
            "--type", "claude-bypass",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agentwire new failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )


def send_prompt_to_worker(session_full_name: str, prompt: str) -> None:
    """Inject the initial prompt into the worker session's pane 0."""
    from agentwire import pane_manager

    pane_manager.send_to_target(f"{session_full_name}.0", prompt, enter=True)


def _within_work_hours(now: datetime, config: MissionsConfig) -> bool:
    return config.work_hours_start <= now.hour < config.work_hours_end


def _build_initial_prompt(
    issue: github.Issue,
    criteria: list[str],
    branch: str,
    repo: RepoConfig,
) -> str:
    """Assemble the initial worker prompt from issue body + criteria."""
    crit = "\n".join(f"- {c}" for c in criteria)
    return (
        f"You are working on issue #{issue.number} in {repo.name}.\n"
        f"\n"
        f"## Issue title\n"
        f"{issue.title}\n"
        f"\n"
        f"## Issue body\n"
        f"{issue.body}\n"
        f"\n"
        f"## Acceptance criteria\n"
        f"{crit}\n"
        f"\n"
        f"## Your task\n"
        f"\n"
        f"You are in an isolated git worktree on branch `{branch}`. Make the\n"
        f"code changes to satisfy the acceptance criteria. Then:\n"
        f"\n"
        f"1. Commit your work with a clear message.\n"
        f"2. Push the branch.\n"
        f"3. Open a DRAFT pull request titled `mission #{issue.number}: <short summary>`\n"
        f"   whose body references the issue (e.g. `Closes #{issue.number}`).\n"
        f"4. Stop and wait for review.\n"
        f"\n"
        f"When you receive review feedback, you'll be sent `/clear` followed by\n"
        f"a context-refresh prompt pointing at a summary file. Address the\n"
        f"feedback, push, and wait again — the PR-feedback router handles each\n"
        f"round. Begin.\n"
    )


# --- public entry point -------------------------------------------------------


def tick(config: MissionsConfig | None = None, *, now: datetime | None = None) -> DispatchReport:
    """One dispatcher tick.

    Args:
        config: Pre-loaded config; if None, loads from disk.
        now: Override for "current time" (testing).
    """
    if config is None:
        config = load_config()
    if now is None:
        now = datetime.now()

    report = DispatchReport(started_at=now.isoformat())

    if not _within_work_hours(now, config):
        report.out_of_hours = True
        state.record_tick("dispatcher")
        return report

    active_sessions = list_mission_sessions()
    per_repo_active: dict[str, int] = {}
    for s in active_sessions:
        parsed = naming.parse_mission_session(s)
        if parsed:
            per_repo_active[parsed[0]] = per_repo_active.get(parsed[0], 0) + 1
    global_active = len(active_sessions)

    for repo_short, repo in config.repos.items():
        if global_active >= config.global_concurrency:
            report.skipped.append({"repo": repo_short, "reason": "global concurrency cap"})
            break

        repo_active = per_repo_active.get(repo_short, 0)
        if repo_active >= repo.per_repo_concurrency:
            report.skipped.append({"repo": repo_short, "reason": "per-repo concurrency cap"})
            continue

        try:
            issues = github.list_issues(repo.name)
        except github.GitHubError as e:
            log.warning("github.list_issues(%s) failed: %s", repo.name, e)
            report.skipped.append({"repo": repo_short, "reason": f"github error: {e}"})
            continue

        eligible_issues: list[github.Issue] = []
        for issue in sorted(issues, key=lambda i: i.number):
            ok, reason = eligibility.is_eligible(issue)
            if ok:
                eligible_issues.append(issue)
            else:
                report.skipped.append(
                    {"repo": repo_short, "issue": issue.number, "reason": reason}
                )

        for issue in eligible_issues:
            if (
                repo_active >= repo.per_repo_concurrency
                or global_active >= config.global_concurrency
            ):
                break
            if _dispatch_one(repo_short, repo, issue, report):
                repo_active += 1
                global_active += 1

    state.record_tick("dispatcher")
    return report


def _dispatch_one(
    repo_short: str,
    repo: RepoConfig,
    issue: github.Issue,
    report: DispatchReport,
) -> bool:
    """Try to dispatch one issue. Returns True on success."""
    slug = naming.slugify(issue.title)
    session_full = naming.session_name(repo_short, issue.number, slug)
    branch = naming.branch_name(issue.number, slug)
    lock_key = f"mission-{issue.number}"

    try:
        with session_lock(lock_key, wait=False):
            try:
                create_worker_session(session_full)
            except RuntimeError as e:
                report.skipped.append(
                    {
                        "repo": repo_short,
                        "issue": issue.number,
                        "reason": f"create_session failed: {e}",
                    }
                )
                return False

            if not wait_for_session_ready(session_full):
                report.skipped.append(
                    {
                        "repo": repo_short,
                        "issue": issue.number,
                        "reason": "session not ready before timeout",
                    }
                )
                return False

            criteria = eligibility.extract_acceptance_criteria(issue.body) or []
            prompt = _build_initial_prompt(issue, criteria, branch, repo)
            send_prompt_to_worker(session_full, prompt)

            try:
                github.comment_issue(
                    repo.name,
                    issue.number,
                    f"Dispatched to mission worker session `{session_full}` "
                    f"on branch `{branch}`.\n\n— mission-dispatcher",
                )
            except github.GitHubError as e:
                log.warning("comment_issue failed for #%d: %s", issue.number, e)

            report.dispatched.append(
                {
                    "repo": repo_short,
                    "issue": issue.number,
                    "session": session_full,
                    "branch": branch,
                }
            )
            return True
    except LockConflict:
        report.skipped.append(
            {
                "repo": repo_short,
                "issue": issue.number,
                "reason": "locked (another dispatcher run in flight)",
            }
        )
        return False
