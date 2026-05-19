"""Mission worktree-janitor: reap sessions + worktrees whose PR is closed.

Stateless. Runs every 6 hours from launchd, or on demand via
``agentwire mission gc``. Walks active mission sessions, looks up the
associated PR by branch, and tears down sessions whose PR is ``MERGED`` or
``CLOSED``. Also sweeps the worktrees parent dir for orphan worktrees
(directories with no matching tmux session).

Side-effecting shell-outs (``agentwire kill``, ``git worktree remove``) live
as module-level functions so tests can monkeypatch them.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agentwire.missions import dispatcher, github, naming, state
from agentwire.missions.config import MissionsConfig, RepoConfig, load_config

log = logging.getLogger(__name__)


@dataclass
class GcReport:
    """Result of one gc tick — useful for CLI/JSON output."""

    started_at: str
    reaped: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    orphans_removed: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "reaped": list(self.reaped),
            "skipped": list(self.skipped),
            "orphans_removed": list(self.orphans_removed),
        }


# --- side-effecting helpers (monkeypatched in tests) --------------------------


def kill_session(session_full_name: str) -> None:
    """Kill a tmux session via ``agentwire kill``.

    Raises ``RuntimeError`` on failure.
    """
    result = subprocess.run(
        ["agentwire", "kill", "-s", session_full_name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agentwire kill failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )


def remove_worktree(path: Path) -> None:
    """Remove a git worktree at ``path`` via ``git worktree remove --force``,
    then delete the local branch it pointed at.

    ``--force`` because by the time gc runs the PR is merged/closed and any
    uncommitted work is moot. Runs from inside the worktree so git can resolve
    the main repo via the worktree's .git pointer. Raises ``RuntimeError`` on
    failure (e.g. broken worktree pointer, or path not registered with git).

    After worktree removal, attempts ``git branch -D <branch>`` from the
    canonical repo so killed missions don't leak local branches. Branch name
    is derived from the worktree directory name (``mission-N-slug``). Branch
    deletion failures are NOT fatal — the worktree is the load-bearing part.
    """
    cwd = str(path) if path.is_dir() else None
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree remove failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )

    # Best-effort branch cleanup. The worktree's parent is `{repo}-worktrees`;
    # the canonical repo lives at `{projects_dir}/{repo}` (one level up + drop
    # the `-worktrees` suffix). Run `git branch -D` from there.
    branch = path.name
    worktrees_parent = path.parent
    canonical_repo = worktrees_parent.parent / worktrees_parent.name.removesuffix("-worktrees")
    if canonical_repo.is_dir():
        subprocess.run(
            ["git", "branch", "-D", branch],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(canonical_repo),
            check=False,
        )


def list_worktree_dirs(parent_dir: Path) -> list[Path]:
    """List mission-* subdirectories under a ``{repo}-worktrees`` parent."""
    if not parent_dir.is_dir():
        return []
    return [p for p in parent_dir.iterdir() if p.is_dir() and p.name.startswith("mission-")]


# --- public entry point -------------------------------------------------------


def gc(config: MissionsConfig | None = None) -> GcReport:
    """One janitor tick.

    For each active mission session, reap it if its PR is MERGED/CLOSED.
    Then sweep each configured repo's worktree parent for orphans.
    """
    if config is None:
        config = load_config()

    report = GcReport(started_at=datetime.now().isoformat())

    active_sessions = dispatcher.list_mission_sessions()
    live_session_names: set[str] = set(active_sessions)
    # Worktree paths gc has already touched this tick — avoid the orphan
    # sweep re-attempting (or re-reporting) a path the session-reap loop
    # already removed (or failed on).
    handled_worktree_paths: set[Path] = set()

    for session_full in active_sessions:
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
        if pr.state == "OPEN":
            report.skipped.append({"session": session_full, "reason": "PR still OPEN"})
            continue

        wt = naming.worktree_path(repo.projects_dir, repo.short, issue_number, slug)
        handled_worktree_paths.add(wt)
        _reap_session(session_full, repo, issue_number, slug, pr, wt, report)
        live_session_names.discard(session_full)

    for repo_short, repo in config.repos.items():
        parent = repo.projects_dir / f"{repo_short}-worktrees"
        for wt_dir in list_worktree_dirs(parent):
            if wt_dir in handled_worktree_paths:
                continue
            session_for_dir = f"{repo_short}/{wt_dir.name}"
            if session_for_dir in live_session_names:
                continue
            try:
                remove_worktree(wt_dir)
                report.orphans_removed.append({"repo": repo_short, "path": str(wt_dir)})
            except RuntimeError as e:
                report.skipped.append(
                    {"orphan": str(wt_dir), "reason": f"worktree remove failed: {e}"}
                )

    state.record_tick("gc")
    return report


def _reap_session(
    session_full: str,
    repo: RepoConfig,
    issue_number: int,
    slug: str,
    pr: github.PullRequest,
    wt: Path,
    report: GcReport,
) -> None:
    """Tear down one mission worker: kill session, remove worktree, clear state."""
    try:
        kill_session(session_full)
    except RuntimeError as e:
        report.skipped.append(
            {"session": session_full, "reason": f"kill failed: {e}"}
        )
        return

    worktree_removed = False
    if wt.exists():
        try:
            remove_worktree(wt)
            worktree_removed = True
        except RuntimeError as e:
            report.skipped.append(
                {"session": session_full, "reason": f"worktree remove failed: {e}"}
            )

    state.forget_pr(pr.number)

    report.reaped.append(
        {
            "session": session_full,
            "issue": issue_number,
            "pr": pr.number,
            "pr_state": pr.state,
            "worktree_removed": worktree_removed,
            "worktree_path": str(wt),
        }
    )
