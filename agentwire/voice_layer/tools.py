"""The buddy's tool surface — READ-ONLY fleet awareness (spike).

This module is the security boundary of the whole voice layer, so it is built
as a hard allowlist rather than a passthrough.

**Why an allowlist and not a generic ``run_agentwire_cmd`` tool.** Voice adds a
failure mode the tool layer has never had: mis-transcription. "kill the worker"
and "kill the worktree" differ by one phoneme; so do most session names. A tool
that took an argv list and ran it would turn every transcription error into an
arbitrary command. Instead every tool here BUILDS its own argv from validated
parameters — the model chooses *which* tool, never *what runs*. A garbled
session name fails the pattern check and returns an error the buddy can read
back, which is the correct outcome: ask again.

**Why read-only in this slice.** Anything routed through a Claude session
inherits damage-control hooks, worktree isolation, posture and prompt routing.
Anything the voice layer does directly inherits none of that. Reads are safe to
do directly; writes are not, and the write story is delegation — spawning and
directing sessions — which is the NEXT slice, not this one. There is
deliberately no escape hatch here for a write: adding one means adding a tool,
in a diff someone reviews.

Everything dispatches through the ``agentwire`` CLI, which is the documented
single source of truth for session logic. The one exception is ``gh``, which
agentwire has no wrapper for.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from ..mcp_core import run_agentwire_cmd
from . import delivery

# Session names as the inbox defines them, plus the optional `@machine` suffix
# a remote session carries. Anchored: a partial match is how a fuzzy
# transcription would slip through.
#
# Every segment must START alphanumeric, which is doing two jobs the obvious
# character class misses (both caught by tests, both real):
#   - `-` is a legal name character, so an unanchored-start pattern accepts
#     `--help` — a name that reaches the CLI as a FLAG, not a value.
#   - `.` is a legal name character, so it accepts `../etc/passwd`.
# A leading separator is never a real session name, so requiring alphanumeric
# first closes both without narrowing anything legitimate.
_SESSION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*"
    r"(?:@[A-Za-z0-9][A-Za-z0-9._-]*)?$"
)

#: `owner/name`, the only form `gh --repo` should ever receive from a model.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

_MAX_OUTPUT_LINES = 200
_MAX_PR_LIMIT = 50


class ToolError(Exception):
    """A tool call was refused — bad arguments, or a tool that doesn't exist."""


def _session_arg(args: dict, key: str = "session") -> str:
    value = args.get(key)
    if (
        not isinstance(value, str)
        or not _SESSION_RE.match(value.strip())
        or ".." in value.split("/")  # belt-and-braces; the pattern already refuses it
    ):
        raise ToolError(
            f"'{value}' is not a valid session name. Ask which session was meant, "
            "then use the exact name from fleet_sessions."
        )
    return value.strip()


def _int_arg(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = args.get(key, default)
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


@dataclass(frozen=True)
class ReadOnlyTool:
    """One callable exposed to the realtime model.

    ``run`` receives the model's (already parsed) arguments and returns a
    JSON-serializable result. It builds its own argv — the arguments never
    reach a shell, and never become a command name or a flag.
    """

    name: str
    description: str
    run: Callable[[dict], dict]
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False}
    )

    def to_realtime_tool(self) -> dict:
        """The OpenAI Realtime ``session.tools[]`` entry for this tool."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


# ---------------------------------------------------------------------------
# Tool implementations — each one owns its argv
# ---------------------------------------------------------------------------


def _fleet_sessions(args: dict) -> dict:
    return run_agentwire_cmd(["list", "--sessions"])


def _fleet_worktrees(args: dict) -> dict:
    return run_agentwire_cmd(["worktree", "--list"])


def _fleet_dangling(args: dict) -> dict:
    return run_agentwire_cmd(["worktree", "--dangling"])


def _fleet_scheduler(args: dict) -> dict:
    return run_agentwire_cmd(["scheduler", "board"])


def _fleet_projects(args: dict) -> dict:
    return run_agentwire_cmd(["projects", "list"])


def _fleet_dead_letters(args: dict) -> dict:
    return run_agentwire_cmd(["msg", "dead"])


def _fleet_session_output(args: dict) -> dict:
    session = _session_arg(args)
    lines = _int_arg(args, "lines", 50, 1, _MAX_OUTPUT_LINES)
    return run_agentwire_cmd(["output", "-s", session, "-n", str(lines)])


def _fleet_pull_requests(args: dict) -> dict:
    """Open PRs via ``gh``. Requires an explicit, validated ``owner/name``.

    Deliberately not defaulted to a cwd: the buddy has no checkout, so there is
    no "current repo" to be wrong about. It learns valid repos from
    ``fleet_projects``.
    """
    repo = args.get("repo")
    if not isinstance(repo, str) or not _REPO_RE.match(repo.strip()):
        raise ToolError(
            "Need a repository as 'owner/name'. Use fleet_projects to see which "
            "projects exist, and confirm the repo with the owner if unsure."
        )
    limit = _int_arg(args, "limit", 20, 1, _MAX_PR_LIMIT)
    cmd = [
        "gh", "pr", "list", "--repo", repo.strip(), "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,author,isDraft,headRefName,updatedAt,url",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "error": f"gh failed: {exc}"}
    if result.returncode != 0:
        return {"success": False, "error": result.stderr.strip()[:500]}
    try:
        return {"success": True, "pull_requests": json.loads(result.stdout or "[]")}
    except json.JSONDecodeError:
        return {"success": False, "error": "gh returned unparseable JSON"}


def _buddy_inbox(args: dict) -> dict:
    """The buddy's OWN mail — what other sessions have reported to it.

    Reads the spool the delivery adapter writes. ``ack`` advances the read
    cursor, so the buddy only marks mail read once it has actually said it out
    loud — an unacked read after a dropped call is re-read, not lost.
    """
    name = args.get("_buddy") or ""
    if not name:
        raise ToolError("buddy identity missing from tool context")
    ack = bool(args.get("ack", False))
    unread_only = bool(args.get("unread_only", True))
    messages = delivery.read_spool(name, unread_only=unread_only, ack=ack)
    return {
        "success": True,
        "count": len(messages),
        "messages": [
            {k: m.get(k) for k in ("id", "from", "kind", "text", "ts", "ref")}
            for m in messages
        ],
    }


READ_ONLY_TOOLS: tuple[ReadOnlyTool, ...] = (
    ReadOnlyTool(
        name="fleet_sessions",
        description=(
            "List every live agentwire session with its role, parent and activity. "
            "This is the answer to 'what is running' and the source of truth for "
            "exact session names."
        ),
        run=_fleet_sessions,
    ),
    ReadOnlyTool(
        name="fleet_worktrees",
        description=(
            "List worktree sessions and their branches, including orphaned worktree "
            "directories left on disk by sessions that have died."
        ),
        run=_fleet_worktrees,
    ),
    ReadOnlyTool(
        name="fleet_dangling",
        description=(
            "Find worker sessions with an OPEN pull request and no live parent — work "
            "that is finished but has nobody positioned to review or merge it. Strong "
            "signal for 'what needs me'."
        ),
        run=_fleet_dangling,
    ),
    ReadOnlyTool(
        name="fleet_scheduler",
        description=(
            "The scheduled-task board: what is due, what is gated, what ran recently "
            "and what failed."
        ),
        run=_fleet_scheduler,
    ),
    ReadOnlyTool(
        name="fleet_projects",
        description="List configured projects and their repository paths.",
        run=_fleet_projects,
    ),
    ReadOnlyTool(
        name="fleet_dead_letters",
        description=(
            "Messages between sessions that failed to deliver and were dropped. A "
            "non-empty result usually means a report-back was lost — worth surfacing."
        ),
        run=_fleet_dead_letters,
    ),
    ReadOnlyTool(
        name="fleet_session_output",
        description=(
            "Read the recent terminal output of ONE session, to see what it is "
            "actually doing or what it is stuck on. Use the exact session name from "
            "fleet_sessions; never guess a name you half-heard."
        ),
        run=_fleet_session_output,
        parameters={
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "Exact session name, as reported by fleet_sessions.",
                },
                "lines": {
                    "type": "integer",
                    "description": f"How many trailing lines to read (1-{_MAX_OUTPUT_LINES}, default 50).",
                },
            },
            "required": ["session"],
            "additionalProperties": False,
        },
    ),
    ReadOnlyTool(
        name="fleet_pull_requests",
        description=(
            "Open pull requests for one repository, including which are drafts. "
            "Requires the repository as 'owner/name'."
        ),
        run=_fleet_pull_requests,
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository as 'owner/name'."},
                "limit": {
                    "type": "integer",
                    "description": f"Maximum PRs to return (1-{_MAX_PR_LIMIT}, default 20).",
                },
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    ),
    ReadOnlyTool(
        name="buddy_inbox",
        description=(
            "Your own mail — reports and requests other sessions have sent YOU. Read "
            "this when asked what needs attention. Set ack to true only once you have "
            "actually told the owner what it says."
        ),
        run=_buddy_inbox,
        parameters={
            "type": "object",
            "properties": {
                "ack": {
                    "type": "boolean",
                    "description": "Mark the returned messages as read. Default false.",
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "Only unread messages. Default true.",
                },
            },
            "additionalProperties": False,
        },
    ),
)

TOOLS_BY_NAME = {t.name: t for t in READ_ONLY_TOOLS}


def realtime_tool_defs() -> list[dict]:
    """The full ``session.tools[]`` array for a Realtime session."""
    return [t.to_realtime_tool() for t in READ_ONLY_TOOLS]


def dispatch(name: str, args: dict, buddy: str) -> dict:
    """Run one tool call by name. Never raises — errors come back as data.

    Returning an error rather than raising is what lets the buddy *say* what
    went wrong ("I don't have a session by that name — which one did you
    mean?") instead of the conversation stalling on an unresolved function
    call.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {
            "success": False,
            "error": f"No such tool: {name}. Available: {', '.join(sorted(TOOLS_BY_NAME))}",
        }
    payload = dict(args or {})
    payload["_buddy"] = buddy
    try:
        return tool.run(payload)
    except ToolError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # a tool failure must never kill the conversation
        return {"success": False, "error": f"{name} failed: {exc}"}
