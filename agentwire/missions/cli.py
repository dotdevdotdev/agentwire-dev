"""CLI handlers for ``agentwire mission ...``.

Thin wrappers over ``dispatcher`` / ``feedback_router`` / ``gc`` / ``github``.
Argparse wiring lives in ``agentwire/__main__.py``; the handlers receive an
``argparse.Namespace`` and return an exit code.

Each handler supports ``--json`` so the portal / MCP layer can shell out and
parse structured output.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agentwire.missions import (
    dispatcher,
    eligibility,
    feedback_router,
    gc,
    github,
    naming,
    state,
)
from agentwire.missions.config import MissionsConfig, load_config

# --- output helpers -----------------------------------------------------------


def _emit(args, payload: dict[str, Any], human: str = "", exit_code: int = 0) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    elif human:
        print(human)
    return exit_code


def _emit_error(args, message: str, exit_code: int = 1) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"success": False, "error": message}, indent=2))
    else:
        print(f"error: {message}", file=sys.stderr)
    return exit_code


# --- shared lookups -----------------------------------------------------------


@dataclass
class _RepoLookup:
    config: MissionsConfig
    repo_short: str
    repo_name: str


def _resolve_repo(args, config: MissionsConfig) -> _RepoLookup | None:
    """Map ``--repo`` (short name) to a full ``owner/repo`` via config.

    Returns ``None`` and prints an error if the short name isn't registered.
    """
    short = getattr(args, "repo", None)
    if not short:
        return None
    repo = config.get_repo(short)
    if repo is None:
        _emit_error(args, f"repo '{short}' not in missions config")
        return None
    return _RepoLookup(config=config, repo_short=short, repo_name=repo.name)


def _find_active_session_for_issue(
    repo_short: str, issue_number: int
) -> tuple[str, str] | None:
    """Return ``(session_full, slug)`` for the running mission session that
    matches ``repo_short`` + ``issue_number``, or ``None`` if no session is
    active.
    """
    for s in dispatcher.list_mission_sessions():
        parsed = naming.parse_mission_session(s)
        if parsed and parsed[0] == repo_short and parsed[1] == issue_number:
            return s, parsed[2]
    return None


# --- handlers -----------------------------------------------------------------


def cmd_mission_list(args) -> int:
    """List eligible-but-unstarted issues + active mission sessions per repo."""
    config = load_config()
    active = dispatcher.list_mission_sessions()
    active_by_repo: dict[str, list[dict]] = {}
    for s in active:
        parsed = naming.parse_mission_session(s)
        if not parsed:
            continue
        repo_short, n, slug = parsed
        active_by_repo.setdefault(repo_short, []).append(
            {"issue": n, "slug": slug, "session": s}
        )

    eligible_by_repo: dict[str, list[dict]] = {}
    errors: list[dict] = []
    for repo_short, repo in config.repos.items():
        try:
            issues = github.list_issues(repo.name)
        except github.GitHubError as e:
            errors.append({"repo": repo_short, "error": str(e)})
            continue
        bucket = []
        for issue in sorted(issues, key=lambda i: i.number):
            ok, reason = eligibility.is_eligible(issue)
            bucket.append(
                {
                    "issue": issue.number,
                    "title": issue.title,
                    "eligible": ok,
                    "reason": reason if not ok else "",
                }
            )
        eligible_by_repo[repo_short] = bucket

    payload = {
        "active": active_by_repo,
        "eligible": eligible_by_repo,
        "errors": errors,
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0

    print("Active mission sessions:")
    if not active_by_repo:
        print("  (none)")
    for repo_short, rows in active_by_repo.items():
        print(f"  {repo_short}:")
        for row in rows:
            print(f"    #{row['issue']:<5} {row['session']}")
    print()
    print("Eligible issues (agent-ready + acceptance criteria):")
    if not eligible_by_repo:
        print("  (no repos configured)")
    for repo_short, rows in eligible_by_repo.items():
        eligibles = [r for r in rows if r["eligible"]]
        print(f"  {repo_short}: {len(eligibles)} eligible")
        for row in eligibles:
            print(f"    #{row['issue']:<5} {row['title']}")
    if errors:
        print()
        print("Errors:")
        for e in errors:
            print(f"  {e['repo']}: {e['error']}")
    return 0


def cmd_mission_show(args) -> int:
    """Show a single issue: body, criteria, dispatch status, PR link."""
    config = load_config()
    lookup = _resolve_repo(args, config)
    if lookup is None:
        return 2

    try:
        issue = github.get_issue(lookup.repo_name, args.number)
    except github.GitHubError as e:
        return _emit_error(args, f"get_issue failed: {e}")

    criteria = eligibility.extract_acceptance_criteria(issue.body) or []
    ok, reason = eligibility.is_eligible(issue)

    active = _find_active_session_for_issue(lookup.repo_short, issue.number)
    session_info = None
    pr_info: dict | None = None
    if active:
        session_full, slug = active
        branch = naming.branch_name(issue.number, slug)
        session_info = {"session": session_full, "branch": branch, "slug": slug}
        try:
            pr = github.get_pr_by_branch(lookup.repo_name, branch)
        except github.GitHubError as e:
            pr_info = {"error": str(e)}
        else:
            if pr is not None:
                pr_info = {
                    "number": pr.number,
                    "state": pr.state,
                    "url": pr.url,
                    "is_draft": pr.is_draft,
                }

    payload = {
        "issue": {
            "number": issue.number,
            "title": issue.title,
            "state": issue.state,
            "labels": list(issue.labels),
            "body": issue.body,
        },
        "acceptance_criteria": criteria,
        "eligible": ok,
        "eligibility_reason": reason,
        "session": session_info,
        "pr": pr_info,
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0

    print(f"#{issue.number} {issue.title}  [{issue.state}]")
    print(f"labels: {', '.join(issue.labels) or '(none)'}")
    print(f"eligible: {ok}{f'  ({reason})' if not ok else ''}")
    print()
    print("Acceptance criteria:")
    if not criteria:
        print("  (none parsed)")
    for c in criteria:
        print(f"  - {c}")
    print()
    if session_info:
        print(f"Active worker: {session_info['session']}")
        if pr_info:
            if "error" in pr_info:
                print(f"PR lookup error: {pr_info['error']}")
            else:
                print(
                    f"PR: #{pr_info['number']} [{pr_info['state']}]"
                    f"  {pr_info['url']}"
                )
        else:
            print("PR: not yet opened")
    else:
        print("Worker: not active")
    return 0


def cmd_mission_status(args) -> int:
    """Summarized per-repo counts: active workers + eligible queue depth."""
    config = load_config()
    active = dispatcher.list_mission_sessions()
    active_by_repo: dict[str, int] = {}
    for s in active:
        parsed = naming.parse_mission_session(s)
        if parsed:
            active_by_repo[parsed[0]] = active_by_repo.get(parsed[0], 0) + 1

    rows: list[dict] = []
    for repo_short, repo in config.repos.items():
        try:
            issues = github.list_issues(repo.name)
            eligible_count = sum(1 for i in issues if eligibility.is_eligible(i)[0])
            err = None
        except github.GitHubError as e:
            eligible_count = 0
            err = str(e)
        rows.append(
            {
                "repo": repo_short,
                "active": active_by_repo.get(repo_short, 0),
                "cap": repo.per_repo_concurrency,
                "eligible": eligible_count,
                "error": err,
            }
        )

    payload = {
        "global_active": len(active),
        "global_cap": config.global_concurrency,
        "work_hours": [config.work_hours_start, config.work_hours_end],
        "repos": rows,
        "last_tick": state.read_last_tick(),
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0

    print(
        f"Mission orchestrator status   (global: {len(active)}/{config.global_concurrency},"
        f" work hours {config.work_hours_start:02d}–{config.work_hours_end:02d})"
    )
    print(f"{'repo':<24} {'active/cap':<12} {'eligible':<10} error")
    for r in rows:
        ac = f"{r['active']}/{r['cap']}"
        err = r["error"] or ""
        print(f"{r['repo']:<24} {ac:<12} {r['eligible']:<10} {err}")
    last = payload["last_tick"]
    if last:
        print()
        for component, ts in last.items():
            print(f"  {component} last ran: {ts}")
    return 0


def cmd_mission_spawn(args) -> int:
    """Force-dispatch a specific issue, bypassing eligibility checks."""
    config = load_config()
    lookup = _resolve_repo(args, config)
    if lookup is None:
        return 2

    try:
        issue = github.get_issue(lookup.repo_name, args.number)
    except github.GitHubError as e:
        return _emit_error(args, f"get_issue failed: {e}")

    repo = config.get_repo(lookup.repo_short)
    report = dispatcher.DispatchReport(started_at=datetime.now().isoformat())
    ok = dispatcher._dispatch_one(lookup.repo_short, repo, issue, report)
    if ok and getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    if ok:
        d = report.dispatched[0]
        print(f"Dispatched #{d['issue']} → {d['session']}  (branch {d['branch']})")
        return 0

    return _emit_error(
        args,
        "dispatch failed: " + "; ".join(s.get("reason", "?") for s in report.skipped),
    )


def cmd_mission_stall(args) -> int:
    """Remove ``agent-ready``, add ``stalled``, comment the reason."""
    config = load_config()
    lookup = _resolve_repo(args, config)
    if lookup is None:
        return 2

    try:
        github.edit_issue_labels(
            lookup.repo_name,
            args.number,
            add=["stalled"],
            remove=["agent-ready"],
        )
        github.comment_issue(
            lookup.repo_name,
            args.number,
            f"Mission stalled: {args.reason}\n\n— `agentwire mission stall`",
        )
    except github.GitHubError as e:
        return _emit_error(args, f"stall failed: {e}")

    return _emit(
        args,
        {"success": True, "issue": args.number, "action": "stalled"},
        human=f"Stalled #{args.number}: {args.reason}",
    )


def cmd_mission_resume(args) -> int:
    """Restore ``agent-ready``, remove ``stalled``."""
    config = load_config()
    lookup = _resolve_repo(args, config)
    if lookup is None:
        return 2

    try:
        github.edit_issue_labels(
            lookup.repo_name,
            args.number,
            add=["agent-ready"],
            remove=["stalled"],
        )
    except github.GitHubError as e:
        return _emit_error(args, f"resume failed: {e}")

    return _emit(
        args,
        {"success": True, "issue": args.number, "action": "resumed"},
        human=f"Resumed #{args.number} — eligible for next dispatcher tick",
    )


def cmd_mission_kill(args) -> int:
    """Manual override: kill the worker + worktree without closing the PR.

    Distinct from ``gc``, which is PR-driven (only tears down sessions whose
    PR is MERGED/CLOSED). Use ``kill`` when you want the operator-side
    teardown without changing GitHub state.
    """
    config = load_config()
    lookup = _resolve_repo(args, config)
    if lookup is None:
        return 2

    active = _find_active_session_for_issue(lookup.repo_short, args.number)
    if active is None:
        return _emit_error(args, f"no active mission session for #{args.number}")

    session_full, slug = active
    repo = config.get_repo(lookup.repo_short)
    wt = naming.worktree_path(repo.projects_dir, repo.short, args.number, slug)

    try:
        gc.kill_session(session_full)
    except RuntimeError as e:
        return _emit_error(args, f"kill_session failed: {e}")

    worktree_removed = False
    worktree_error: str | None = None
    if wt.exists():
        try:
            gc.remove_worktree(wt)
            worktree_removed = True
        except RuntimeError as e:
            worktree_error = str(e)

    payload = {
        "success": True,
        "issue": args.number,
        "session": session_full,
        "worktree": str(wt),
        "worktree_removed": worktree_removed,
        "worktree_error": worktree_error,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(f"Killed {session_full}")
        if worktree_removed:
            print(f"Removed worktree {wt}")
        elif worktree_error:
            print(f"Worktree remove failed: {worktree_error}", file=sys.stderr)
    return 0


def cmd_mission_gc(args) -> int:
    """Run the worktree-janitor synchronously."""
    report = gc.gc()
    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"Reaped: {len(report.reaped)}, orphans removed: {len(report.orphans_removed)}, "
              f"skipped: {len(report.skipped)}")
        for r in report.reaped:
            print(f"  reap   {r['session']}  (PR #{r['pr']} {r['pr_state']})")
        for o in report.orphans_removed:
            print(f"  orphan {o['path']}")
        for s in report.skipped:
            tag = s.get("session") or s.get("orphan") or "?"
            print(f"  skip   {tag}: {s.get('reason', '?')}")
    return 0


def cmd_mission_tick(args) -> int:
    """Run the dispatcher synchronously."""
    report = dispatcher.tick()
    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2))
    else:
        if report.out_of_hours:
            print("Out of work hours — no dispatch.")
        print(f"Dispatched: {len(report.dispatched)}, skipped: {len(report.skipped)}")
        for d in report.dispatched:
            print(f"  spawn  #{d['issue']} → {d['session']}")
        for s in report.skipped:
            tag = f"#{s['issue']}" if "issue" in s else s.get("repo", "?")
            print(f"  skip   {tag}: {s.get('reason', '?')}")
    return 0


def cmd_mission_route_feedback(args) -> int:
    """Run the PR-feedback router synchronously."""
    report = feedback_router.route_feedback()
    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"Routed: {len(report.routed)}, skipped: {len(report.skipped)}")
        for r in report.routed:
            print(f"  route  PR #{r['pr']} → {r['session']}  ({r['reviews_routed']} review(s))")
        for s in report.skipped:
            print(f"  skip   {s.get('session', '?')}: {s.get('reason', '?')}")
    return 0


def cmd_mission_init(args) -> int:
    """Create the ``agent-ready`` and ``stalled`` labels on a target repo (idempotent).

    Both labels are required for the mission lifecycle: ``agent-ready`` gates the
    dispatcher, ``stalled`` is applied by ``mission stall``. Without ``stalled``
    pre-created, ``mission stall`` would fail with "'stalled' not found" on a
    fresh repo.
    """
    config = load_config()
    # Allow either short name (looked up via config) or owner/repo form.
    short = args.repo
    repo = config.get_repo(short)
    repo_name = repo.name if repo is not None else short
    if "/" not in repo_name:
        return _emit_error(
            args,
            f"'{short}' is not a configured repo short name and not in 'owner/repo' form",
        )

    labels_spec = [
        ("agent-ready", "0e8a16", "Eligible for mission-dispatcher to pick up."),
        ("stalled", "d93f0b", "Paused by `agentwire mission stall` — not eligible."),
    ]
    results: list[dict] = []
    for name, color, description in labels_spec:
        try:
            created = github.create_label(repo_name, name, color=color, description=description)
        except github.GitHubError as e:
            return _emit_error(args, f"create_label '{name}' failed: {e}")
        results.append({"label": name, "created": created})

    summary = ", ".join(
        f"'{r['label']}' {'created' if r['created'] else 'exists'}" for r in results
    )
    return _emit(
        args,
        {"success": True, "repo": repo_name, "labels": results},
        human=f"{summary} on {repo_name}",
    )
