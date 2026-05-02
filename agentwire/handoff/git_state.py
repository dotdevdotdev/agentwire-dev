"""
Git state capture for handoff bundles.

No existing utility in the codebase wraps `git status/diff/log`; this is fresh.
All functions tolerate non-git directories (return empty strings).
"""

from __future__ import annotations

from pathlib import Path

from ..utils.subprocess import run_command


def _git(args: list[str], cwd: str | Path) -> str:
    result = run_command(["git", *args], cwd=str(cwd), timeout=10)
    if not result.success:
        return ""
    return result.stdout.strip()


def is_git_repo(cwd: str | Path) -> bool:
    return _git(["rev-parse", "--git-dir"], cwd) != ""


def status(cwd: str | Path) -> str:
    """Short porcelain status, or empty string if clean / not a repo."""
    return _git(["status", "--short"], cwd)


def diff(cwd: str | Path, staged: bool = False, max_lines: int = 2000) -> str:
    """Unified diff. Truncates at max_lines to keep bundles bounded."""
    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    out = _git(args, cwd)
    if not out:
        return ""
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append(f"[... diff truncated at {max_lines} lines ...]")
    return "\n".join(lines)


def log(cwd: str | Path, limit: int = 20) -> str:
    return _git(["log", f"-{limit}", "--oneline", "--no-color"], cwd)


def branch(cwd: str | Path) -> str | None:
    out = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out or None


def commit(cwd: str | Path) -> str | None:
    out = _git(["rev-parse", "HEAD"], cwd)
    return out or None


def remote_url(cwd: str | Path) -> str | None:
    out = _git(["config", "--get", "remote.origin.url"], cwd)
    return out or None


def snapshot(cwd: str | Path) -> dict[str, str | None]:
    """One-shot capture of all relevant git state."""
    if not is_git_repo(cwd):
        return {
            "is_repo": False,
            "branch": None,
            "commit": None,
            "remote_url": None,
            "status": "",
            "diff": "",
            "diff_staged": "",
            "log": "",
        }
    return {
        "is_repo": True,
        "branch": branch(cwd),
        "commit": commit(cwd),
        "remote_url": remote_url(cwd),
        "status": status(cwd),
        "diff": diff(cwd),
        "diff_staged": diff(cwd, staged=True),
        "log": log(cwd),
    }
