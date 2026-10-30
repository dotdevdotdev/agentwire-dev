"""The voice assistant's system digest (#1198, slice 4).

What ``agentwire-voice`` knows without being asked: every session with its
live state (busy / idle / blocked), every project with a one-line overview, the
scheduler board, and the notable bits of config. It reuses the buddy's
budgeted roster (:func:`agentwire.voice_layer.digest.build_roster`) rather
than a second fleet summary, and adds what a Claude Code session needs that
the buddy's does not: its own tool and skill names.

Injected at session start (the ``SessionStart`` speech hook prints it, which
Claude Code adds to the context; also on resume and after compaction) and on
demand (``agentwire voice digest``). Budgeted: each section has a cap and the
whole digest :data:`MAX_CHARS`. A section that fails says so in one line and
the rest still ships.
"""

from __future__ import annotations

import time
from typing import Callable

from .core import run_agentwire_cmd
from .voice_layer.digest import build_roster, overview_line

MAX_CHARS = 9000
MAX_SCHEDULER_ROWS = 8

#: Truncation hints in the roster name THIS session's tools.
LIST_TOOLS = ("sessions_list", "projects_list")

TOOLS_INDEX = """\
Look deeper (agentwire MCP tools; load deferred ones with ToolSearch):
- sessions_list, session_output(session), session_info(session): any session in depth.
- projects_list, worktree_list, scheduler_board / scheduler_status / scheduler_history.
- msg_send(to, text): message a session politely. worktree_create(name, project_dir, prompt): start work.
- wiki_query(question): past investigations. history_list: past conversations.
- `agentwire list --state`: live busy / idle / blocked for every session.
- `agentwire voice digest`: this whole digest, fresh. Re-run it before answering "what's busy" if it's stale.
Skills (read before using a command you are unsure of): agentwire-cli, agentwire-mcp-tools,
agentwire-config, agentwire-scheduler, agentwire-project-config."""


def scheduler_section(fetch: Callable[[list[str]], dict]) -> str:
    """The board in a few lines: in flight, overdue, last failures."""
    try:
        data = fetch(["scheduler", "board"])
    except Exception as exc:  # noqa: BLE001  # a section failure never sinks the digest
        return f"SCHEDULER: unavailable ({exc})"
    if not isinstance(data, dict) or data.get("success") is False:
        error = str((data or {}).get("error") or "unavailable").split("\n")[0]
        return f"SCHEDULER: {'no board configured' if 'not found' in error else error}"
    tasks = [t for t in data.get("tasks") or [] if isinstance(t, dict)]
    enabled = [t for t in tasks if t.get("enabled")]
    lines = [f"SCHEDULER ({len(tasks)} tasks, {len(enabled)} enabled):"]
    groups = (
        ("in flight", [t for t in enabled if t.get("in_flight")]),
        ("overdue", sorted((t for t in enabled if (t.get("overdue_by") or 0) > 0 and not t.get("in_flight")),
                           key=lambda t: -(t.get("overdue_by") or 0))),
        ("last run failed", [t for t in enabled
                             if str(t.get("last_status") or "").lower() in ("failed", "failure", "error", "timeout")]),
    )
    rows = 0
    for label, group in groups:
        if not group:
            continue
        shown = group[:MAX_SCHEDULER_ROWS - rows]
        names = ", ".join(str(t.get("name")) + (f" ({t['overdue_str']})" if label == "overdue"
                                                 and t.get("overdue_str") else "") for t in shown)
        more = f", +{len(group) - len(shown)} more" if len(group) > len(shown) else ""
        lines.append(f"- {label}: {names}{more}")
        rows += len(shown)
        if rows >= MAX_SCHEDULER_ROWS:
            break
    if len(lines) == 1:
        lines.append("- nothing in flight, overdue or failing")
    return "\n".join(lines)


def config_section(config) -> str:
    """Notable config in one short block — what changes how things behave."""
    try:
        from .config import enabled_beta_flags

        bits = [
            f"TTS {config.tts.backend}, STT {config.stt.backend}",
            f"projects dir {config.projects.dir}",
        ]
        custom = [s.name for s in config.services.custom]
        if custom:
            bits.append("custom services: " + ", ".join(custom))
        channels = sorted(k for k, v in (config.channels or {}).items() if v)
        if channels:
            bits.append("outbound channels: " + ", ".join(channels))
        beta = sorted(enabled_beta_flags())
        if beta:
            bits.append("beta: " + ", ".join(beta))
        return "CONFIG: " + "; ".join(bits) + ". Details: `agentwire config` / the agentwire-config skill."
    except Exception as exc:  # noqa: BLE001
        return f"CONFIG: unavailable ({exc})"


def build_assistant_digest(
    fetch: Callable[[list[str]], dict] = run_agentwire_cmd,
    now: "float | None" = None,
    overview: Callable[[str], str] = overview_line,
    config=None,
) -> str:
    """The whole digest. Never raises; never longer than :data:`MAX_CHARS`."""
    if config is None:
        from .config import load_config

        config = load_config()
    now = time.time() if now is None else now
    parts = [
        build_roster(fetch, now, overview, list_tools=LIST_TOOLS),
        scheduler_section(fetch),
        config_section(config),
        TOOLS_INDEX,
    ]
    digest = "\n\n".join(parts)
    if len(digest) > MAX_CHARS:
        digest = digest[:MAX_CHARS].rsplit("\n", 1)[0] + "\n…digest truncated (agentwire voice digest)"
    return digest
