"""CLI for repo inspection — ``agentwire repo-info`` / ``agentwire branches``.

The portal's "new session" / "worktree" flows need to know whether a path is a
git repo (and on which branch) and which branches already match a prefix, for
both the local machine and remote machines registered in ``machines.json``.

Per the CLAUDE.md SSOT rule, that git logic lives here in the CLI. The portal
endpoints (``api_check_path`` / ``api_check_branches``) are thin wrappers that
shell out to these commands and parse the ``--json`` output — they no longer
embed forked local/SSH git logic.

Output shapes (``--json``)::

    repo-info: {"exists": bool, "is_git": bool, "current_branch": str | null}
    branches:  {"existing": [branch names]}

Local logic mirrors the old inline portal code exactly: ``expanduser().resolve()``,
``.git`` existence, ``git rev-parse --abbrev-ref HEAD`` / ``git branch --list``.
Remote logic runs the same shell commands over SSH via ``core._run_remote`` (which
resolves the target ``user@host`` from ``machines.json``), keeping the portal's
"stdout only on success, else empty" semantics.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from .core import _output_json, _run_remote


def _remote_stdout(machine_id: str, command: str) -> str:
    """Run ``command`` on ``machine_id`` via SSH, returning stdout on success.

    Mirrors the portal's old ``_run_ssh_command`` contract: stdout when the
    remote command exits 0, otherwise an empty string.
    """
    result = _run_remote(machine_id, command)
    return result.stdout if result.returncode == 0 else ""


def _repo_info(path: str, machine: str) -> dict:
    """Compute {exists, is_git, current_branch} for a local or remote path."""
    if machine and machine != "local":
        exists = "exists" in _remote_stdout(
            machine, f"test -d {shlex.quote(path)} && echo exists"
        )
        is_git = False
        current_branch = None

        if exists:
            is_git = "git" in _remote_stdout(
                machine, f"test -d {shlex.quote(path)}/.git && echo git"
            )
            if is_git:
                branch = _remote_stdout(
                    machine,
                    f"cd {shlex.quote(path)} && git rev-parse --abbrev-ref HEAD",
                )
                current_branch = branch.strip() if branch else None
    else:
        expanded = Path(path).expanduser().resolve()
        exists = expanded.exists() and expanded.is_dir()
        is_git = exists and (expanded / ".git").exists()
        current_branch = None

        if is_git:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=expanded,
                capture_output=True,
                text=True,
            )
            current_branch = result.stdout.strip() if result.returncode == 0 else None

    return {"exists": exists, "is_git": is_git, "current_branch": current_branch}


def _branches(path: str, machine: str, prefix: str) -> list[str]:
    """Existing branch names matching ``prefix`` for a local or remote repo."""
    if machine and machine != "local":
        cmd = (
            f"cd {shlex.quote(path)} && git branch --list "
            f"{shlex.quote(prefix + '*')} --format='%(refname:short)'"
        )
        out = _remote_stdout(machine, cmd)
        branches = out.strip().split("\n") if out else []
    else:
        expanded = Path(path).expanduser().resolve()
        if not expanded.exists():
            return []
        result = subprocess.run(
            ["git", "branch", "--list", f"{prefix}*", "--format=%(refname:short)"],
            cwd=expanded,
            capture_output=True,
            text=True,
        )
        branches = result.stdout.strip().split("\n") if result.returncode == 0 else []

    return [b for b in branches if b]


def cmd_repo_info(args) -> int:
    """Report whether a path exists / is a git repo and its current branch."""
    json_mode = getattr(args, "json", False)
    if not args.path:
        info = {"exists": False, "is_git": False, "current_branch": None}
    else:
        info = _repo_info(args.path, args.machine)

    if json_mode:
        _output_json(info)
    else:
        print(f"exists:         {info['exists']}")
        print(f"is_git:         {info['is_git']}")
        print(f"current_branch: {info['current_branch']}")
    return 0


def cmd_branches(args) -> int:
    """List existing branch names matching a prefix."""
    json_mode = getattr(args, "json", False)
    if not args.path:
        existing: list[str] = []
    else:
        existing = _branches(args.path, args.machine, args.prefix)

    if json_mode:
        _output_json({"existing": existing})
    else:
        for b in existing:
            print(b)
    return 0


def register_repo_parser(subparsers) -> None:
    info_parser = subparsers.add_parser(
        "repo-info",
        help="Check if a path exists / is a git repo and its current branch (local or remote)",
    )
    info_parser.add_argument("--path", required=True, help="Path to check")
    info_parser.add_argument(
        "--machine", default="local", help="Machine ID ('local' or a remote from machines.json)"
    )
    info_parser.add_argument("--json", action="store_true", help="Output JSON")
    info_parser.set_defaults(func=cmd_repo_info)

    branches_parser = subparsers.add_parser(
        "branches",
        help="List existing branch names matching a prefix (local or remote)",
    )
    branches_parser.add_argument("--path", required=True, help="Git repo path")
    branches_parser.add_argument(
        "--machine", default="local", help="Machine ID ('local' or a remote from machines.json)"
    )
    branches_parser.add_argument("--prefix", default="", help="Branch name prefix filter")
    branches_parser.add_argument("--json", action="store_true", help="Output JSON")
    branches_parser.set_defaults(func=cmd_branches)
