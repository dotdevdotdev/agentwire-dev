"""The buddy's opening situational awareness — a bounded roster (#1179).

The first real owner session found a buddy that knew nothing until it called a
tool, and answered "I can't see that" about things it could have looked up.
The owner's spec for the fix: baseline awareness AT ALL TIMES — every open
session and every configured project, each with what's live in it and its
recent activity — plus the short list of tools that deep-dive any of it. Deep
dives stay tool calls; nothing per-project is preloaded beyond one line.

**Budgeted, because the fleet is not.** Every section has a line cap and the
whole digest a character cap (:data:`MAX_CHARS`). Rows are ranked by recency
(most recent activity first), so truncation drops the quietest rows and says
how many, naming the tool that lists them all. Projects with nothing live and
no recent activity collapse to names on one line.

Built at mint time and appended to the instructions. A section that fails to
build says so in one line and the rest still ships — a roster failure must
never cost the owner the session.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..core import run_agentwire_cmd

MAX_CHARS = 6000
MAX_PROJECT_ROWS = 20
MAX_SESSION_ROWS = 25
MAX_OVERVIEW_CHARS = 90
ACTIVITY_HOURS = 24
ACTIVITY_LIMIT = 300

#: Deep-dive tools, one line each. Static on purpose: the descriptions in the
#: tool definitions carry the detail; this is the index the model reads first.
TOOLS_INDEX = """\
Deep dives (call them; never preload):
- project_lookup(project, lookup): GitHub issues/PRs/CI/repo and git log/status/branches for a project.
- project_context(project, part): a project's CLAUDE.md, memory index, skills, tasks.
- fleet_session_output / fleet_session_info / fleet_session_inbox: one session in depth.
- fleet_activity: what happened recently. fleet_dangling: finished work nobody can merge.
- fleet_worktrees, fleet_scheduler(+_status/_history/_live), fleet_tasks, fleet_dead_letters.
- fleet_wiki_search: past investigations. fleet_history: past conversations.
- buddy_inbox / buddy_sent: your own mail and sends. propose_session_message: the one write."""

#: How work flows here, so the buddy can speak about it without a lookup.
WORKFLOWS = """\
How this fleet works: an orchestrator per project directs worktree sessions \
(`agentwire worktree <name> -p <repo>`), each on its own branch, finishing with \
a draft PR and a report-back; the orchestrator reviews and merges. Workers \
report by `agentwire msg` (polite, lands when the box is empty). Scheduled \
tasks run from the scheduler board. GitHub issues are the backlog; PRs carry \
`Closes #N`. Skills a session loads: agentwire-cli, agentwire-mcp-tools, \
agentwire-config, agentwire-project-config, agentwire-scheduler, \
agentwire-desktop-ui, wiki."""


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


#: First lines that describe the FILE rather than the project — Claude Code's
#: own boilerplate and pointers elsewhere. Skipped, not returned.
_BOILERPLATE = re.compile(
    r"(guidance|instructions) (for|to) (claude|ai|agents)|this file provides|"
    r"bootstrapped with|^read [\w./-]+\.md|^see [\w./-]+\.md",
    re.IGNORECASE,
)


def overview_line(project_path: str) -> str:
    """One line about a project: the first prose line of README.md, then
    CLAUDE.md, then AGENTS.md (what an ``@AGENTS.md`` import points at).
    Headings, blanks, fences, lists, imports and boilerplate are skipped."""
    for name in ("README.md", "CLAUDE.md", "AGENTS.md"):
        try:
            text = (Path(project_path) / name).read_text(errors="replace")[:4000]
        except OSError:
            continue
        fenced = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("```"):
                fenced = not fenced
                continue
            if fenced or not line or line[0] in "#-*|>![<@" or _BOILERPLATE.search(line):
                continue
            line = line.replace("**", "")
            return line if len(line) <= MAX_OVERVIEW_CHARS else line[:MAX_OVERVIEW_CHARS - 1] + "…"
    return ""


def _session_project(session: dict, projects: list[dict]) -> str:
    path = str(session.get("path") or "")
    name = str(session.get("name") or "")
    for p in projects:
        ppath, pname = str(p.get("path") or ""), str(p.get("name") or "")
        if ppath and (path == ppath or path.startswith(ppath + "/")):
            return pname
        if pname and f"/worktrees/{pname}/" in path + "/":
            return pname
    for p in projects:
        if name.startswith(f"{p.get('name')}-"):
            return str(p.get("name"))
    return ""


def build_digest(
    fetch: Callable[[list[str]], dict] = run_agentwire_cmd,
    now: "float | None" = None,
    overview: Callable[[str], str] = overview_line,
) -> str:
    """The roster block appended to the buddy's instructions. Never raises."""
    now = time.time() if now is None else now
    errors: list[str] = []

    def get(argv: list[str], key: str) -> list[dict]:
        try:
            data = fetch(argv)
        except Exception as exc:  # a roster failure must never block a mint
            errors.append(f"{key} unavailable ({exc})")
            return []
        if not isinstance(data, dict) or data.get("success") is False:
            errors.append(f"{key} unavailable")
            return []
        rows = data.get(key)
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    sessions = get(["list", "--sessions"], "sessions")
    projects = get(["projects", "list", "--machine", "local"], "projects")
    activity = get(
        ["activity", "list", "--hours", str(ACTIVITY_HOURS), "--limit", str(ACTIVITY_LIMIT)],
        "activity",
    )

    # Newest event per session (the ledger is newest-first, but don't rely on it).
    last: dict[str, tuple[float, str]] = {}
    for e in activity:
        who = str(e.get("session") or e.get("subject") or "")
        ts = _ts(e.get("ts"))
        if who and ts > last.get(who, (0.0, ""))[0]:
            last[who] = (ts, str(e.get("event") or "").replace("_", " "))

    services = [s for s in sessions if s.get("service")]
    agents = [s for s in sessions if not s.get("service")]
    unhealthy = [s for s in services if s.get("service_healthy") is False]
    by_project: dict[str, list[dict]] = {}
    for s in agents:
        by_project.setdefault(_session_project(s, projects), []).append(s)

    # --- sessions -----------------------------------------------------------
    def session_key(s):
        return -last.get(str(s.get("name")), (0.0, ""))[0]

    lines = [f"<fleet_roster as_of=\"{datetime.fromtimestamp(now).strftime('%H:%M')}\">",
             "A snapshot from when this conversation started — re-check with tools for anything current.",
             f"SESSIONS ({len(agents)} agents; {len(services)} services"
             + (f", UNHEALTHY: {', '.join(str(s.get('name')) for s in unhealthy)}" if unhealthy else (", all healthy" if services else ""))
             + "):"]
    ranked = sorted(agents, key=session_key)
    for s in ranked[:MAX_SESSION_ROWS]:
        name = str(s.get("name"))
        bits = [s.get("role") or "session"]
        if s.get("parent"):
            bits.append(f"under {s['parent']}")
        proj = _session_project(s, projects)
        if proj:
            bits.append(f"in {proj}")
        if name in last:
            ts, ev = last[name]
            bits.append(f"{ev} {_age(now - ts)} ago")
        lines.append(f"- {name}: " + ", ".join(bits))
    if len(ranked) > MAX_SESSION_ROWS:
        lines.append(f"- +{len(ranked) - MAX_SESSION_ROWS} more (fleet_sessions)")

    # --- projects -----------------------------------------------------------
    # Activity counts toward a project even when its session has since closed:
    # attribution is by the session's name, not its liveness.
    proj_recent: dict[str, float] = {}
    for who, (ts, _ev) in last.items():
        live = next((s for s in agents if s.get("name") == who), {"name": who})
        proj = _session_project(live, projects)
        if proj and ts > proj_recent.get(proj, 0.0):
            proj_recent[proj] = ts

    ranked_projects = sorted(
        projects,
        key=lambda p: (-proj_recent.get(str(p.get("name")), 0.0),
                       -len(by_project.get(str(p.get("name")), [])),
                       str(p.get("name"))),
    )
    lines.append(f"PROJECTS ({len(projects)}, most recently active first):")
    for p in ranked_projects[:MAX_PROJECT_ROWS]:
        name = str(p.get("name"))
        members = by_project.get(name, [])
        row = f"- {name} ({_home(str(p.get('path') or ''))})"
        about = overview(str(p.get("path") or ""))
        if about:
            row += f": {about}"
        state = [f"{len(members)} live"] if members else []
        recent = proj_recent.get(name)
        if recent:
            state.append(f"active {_age(now - recent)} ago")
        if state:
            row += f" [{', '.join(state)}]"
        lines.append(row)
    rest = ranked_projects[MAX_PROJECT_ROWS:]
    if rest:
        lines.append("- also (fleet_projects): " + ", ".join(str(p.get("name")) for p in rest))

    for err in errors:
        lines.append(f"({err} — use the fleet tools)")
    lines.append("</fleet_roster>")

    roster = "\n".join(lines)
    if len(roster) > MAX_CHARS:
        cut = roster[:MAX_CHARS].rsplit("\n", 1)[0]
        roster = cut + "\n- …roster truncated (fleet_sessions / fleet_projects)\n</fleet_roster>"
    return "\n\n".join([roster, TOOLS_INDEX, WORKFLOWS])
