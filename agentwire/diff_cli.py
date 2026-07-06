"""CLI for the mobile Review window — ``agentwire diff -s <session>``.

The portal's "Review" WinBox window shows what an agent changed and lets a
human tap Approve / Request-changes on a phone. Per the CLAUDE.md SSOT rule,
the diff itself is computed here in the CLI; the MCP tool and the portal are
thin wrappers that parse this command's ``--json`` output.

Output shape (``--json``)::

    {
      "success": true,
      "session": "...",
      "path": "/abs/worktree/path",
      "base": "HEAD" | "origin/main" | "<ref>",
      "files": [
        {
          "path": "b/path", "old_path": "a/path",
          "status": "modified|added|deleted|renamed|binary",
          "additions": N, "deletions": M, "binary": false,
          "hunks": [
            {"header": "@@ ... @@", "section": "func foo()",
             "lines": [{"type": "context|add|del", "content": "...",
                        "old_n": 12, "new_n": 12}]}
          ]
        }
      ],
      "additions": N, "deletions": M, "truncated": false
    }

Base resolution (no ``--base``): if the worktree has uncommitted changes the
diff is vs ``HEAD`` (what the agent just did, not yet committed); otherwise it
is vs ``origin/main`` (a worktree session's committed branch work). The portal
relies on this so a phone review needs zero flags.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# Keep the JSON payload (and the phone DOM that renders it) bounded. A diff
# this large is better reviewed at a desk anyway.
MAX_DIFF_LINES = 4000

_HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)")
_GIT_HEADER_RE = re.compile(r"diff --git a/(.*) b/(.*)")


def _resolve_path(session: str) -> "Path | None":
    """The session's working directory — same resolver the rest of the CLI uses."""
    from .core import _get_session_project_path

    return _get_session_project_path(session)


def _run_git(path: Path, args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _has_uncommitted(path: Path) -> bool:
    result = _run_git(path, ["status", "--porcelain"])
    return result.returncode == 0 and bool(result.stdout.strip())


def _resolve_base(path: Path, explicit: "str | None") -> str:
    if explicit:
        return explicit
    if _has_uncommitted(path):
        return "HEAD"
    if _run_git(path, ["rev-parse", "--verify", "--quiet", "origin/main"]).returncode == 0:
        return "origin/main"
    return "HEAD"


def parse_unified_diff(text: str, max_lines: int = MAX_DIFF_LINES) -> "tuple[list[dict], bool]":
    """Parse ``git diff --no-color`` output into structured per-file hunks."""
    lines = text.split("\n")
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True

    files: list[dict] = []
    cur: "dict | None" = None
    hunk: "dict | None" = None
    old_n = new_n = 0

    for line in lines:
        if line.startswith("diff --git"):
            cur = {
                "path": None,
                "old_path": None,
                "status": "modified",
                "additions": 0,
                "deletions": 0,
                "binary": False,
                "hunks": [],
            }
            files.append(cur)
            hunk = None
            m = _GIT_HEADER_RE.match(line)
            if m:
                cur["old_path"] = m.group(1)
                cur["path"] = m.group(2)
            continue
        if cur is None:
            continue
        if line.startswith("new file"):
            cur["status"] = "added"
            continue
        if line.startswith("deleted file"):
            cur["status"] = "deleted"
            continue
        if line.startswith("rename from "):
            cur["old_path"] = line[len("rename from "):]
            cur["status"] = "renamed"
            continue
        if line.startswith("rename to "):
            cur["path"] = line[len("rename to "):]
            cur["status"] = "renamed"
            continue
        if line.startswith("Binary files"):
            cur["binary"] = True
            cur["status"] = "binary"
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            p = line[4:]
            if p.startswith("b/"):
                cur["path"] = p[2:]
            continue
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            old_n = int(m.group(1)) if m else 0
            new_n = int(m.group(2)) if m else 0
            hunk = {"header": line, "section": (m.group(3).strip() if m else ""), "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None:
            continue
        if line.startswith("+"):
            hunk["lines"].append({"type": "add", "content": line[1:], "new_n": new_n})
            new_n += 1
            cur["additions"] += 1
        elif line.startswith("-"):
            hunk["lines"].append({"type": "del", "content": line[1:], "old_n": old_n})
            old_n += 1
            cur["deletions"] += 1
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            content = line[1:] if line.startswith(" ") else line
            hunk["lines"].append(
                {"type": "context", "content": content, "old_n": old_n, "new_n": new_n}
            )
            old_n += 1
            new_n += 1

    return files, truncated


def _emit_error(json_mode: bool, message: str) -> int:
    if json_mode:
        print(json.dumps({"success": False, "error": message}))
    else:
        print(message)
    return 1


def cmd_diff(args) -> int:
    """Emit a session's git diff as structured JSON (or a human summary)."""
    session = args.session
    json_mode = getattr(args, "json", False)

    path = _resolve_path(session)
    if path is None or not path.exists():
        return _emit_error(
            json_mode, f"Could not resolve a working directory for session '{session}'"
        )
    if _run_git(path, ["rev-parse", "--git-dir"]).returncode != 0:
        return _emit_error(json_mode, f"{path} is not a git repository")

    base = _resolve_base(path, getattr(args, "base", None))
    result = _run_git(path, ["diff", "--no-color", base])
    if result.returncode != 0:
        return _emit_error(json_mode, result.stderr.strip() or "git diff failed")

    files, truncated = parse_unified_diff(result.stdout)
    additions = sum(f["additions"] for f in files)
    deletions = sum(f["deletions"] for f in files)
    payload = {
        "success": True,
        "session": session,
        "path": str(path),
        "base": base,
        "files": files,
        "additions": additions,
        "deletions": deletions,
        "truncated": truncated,
    }

    if json_mode:
        print(json.dumps(payload))
        return 0

    if not files:
        print(f"No changes vs {base}")
        return 0
    for f in files:
        print(f"{f['status'][:3].upper():3}  {f['path']}  +{f['additions']} -{f['deletions']}")
    print(f"\n{len(files)} file(s) changed vs {base}  (+{additions} -{deletions})")
    if truncated:
        print(f"[diff truncated at {MAX_DIFF_LINES} lines]")
    return 0


def register_diff_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "diff",
        help="Structured git diff for a session (drives the mobile Review window)",
    )
    parser.add_argument("-s", "--session", required=True, help="Session name")
    parser.add_argument(
        "--base",
        help="Diff base ref (default: HEAD if uncommitted changes, else origin/main)",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.set_defaults(func=cmd_diff)
