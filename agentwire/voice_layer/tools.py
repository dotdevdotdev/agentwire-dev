"""The buddy's tool surface — the allowlist and the one dispatcher (spike).

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

**Reads are direct; writes go through the spine.** Anything routed through a
Claude session inherits damage-control hooks, worktree isolation, posture and
prompt routing. Anything the voice layer does directly inherits none of that,
so reads happen here and writes (:mod:`~agentwire.voice_layer.write_tools`)
are declared specs gated below the model by
:mod:`~agentwire.voice_layer.confirm` — the canonical one being a message to
a session that already has those guards. Which capabilities may ever appear
here at all is ruled by the tier audit in
:mod:`~agentwire.voice_layer.surface`. There is still deliberately no escape
hatch: adding a capability means adding a tool, in a diff someone reviews.

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

from ..core import run_agentwire_cmd
from . import delivery, outbox
from .confirm import strip_controls

# Session names as the inbox defines them. Anchored: a partial match is how a
# fuzzy transcription would slip through.
#
# Every segment must START alphanumeric, which is doing two jobs the obvious
# character class misses (both caught by tests, both real):
#   - `-` is a legal name character, so an unanchored-start pattern accepts
#     `--help` — a name that reaches the CLI as a FLAG, not a value.
#   - `.` is a legal name character, so it accepts `../etc/passwd`.
# A leading separator is never a real session name, so requiring alphanumeric
# first closes both without narrowing anything legitimate.
#
# `@` is admitted inside a segment. Remote `name@machine` targets are out of
# scope (owner ruling, 2026-08-09) — but `@` does NOT mean
# remote, and a gate keyed on the character told the owner a true local name
# was unreachable. tmux accepts `@` verbatim (only `.` and `:` are rewritten,
# #878) and `inbox._SESSION_RE` admits it, so `ops@edge` is a creatable,
# addressable LOCAL session, and refusing it as "remote" is a confident
# falsehood the owner has no move from — the expensive failure with no screen.
#
# So the SHAPE is validated here and the RULING is enforced by liveness in
# `_session_arg`: a whole name tmux reports live is local by demonstration.
# What that refuses is precisely a name nothing local answers to, which is
# every genuinely remote target, said without claiming to know why.
_SESSION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._@-]*(?:/[A-Za-z0-9][A-Za-z0-9._@-]*)*$"
)

#: `owner/name`, the only form `gh --repo` should ever receive from a model.
#: The leading-alphanumeric rule binds the OWNER only, which is where GitHub
#: actually constrains it (logins are alphanumeric-and-hyphen). REPOSITORY
#: names may begin with `.`, `_` or `-` — `github/.github` is real — so
#: extending the rule across the slash for symmetry would refuse real
#: repositories to close a value-position flag the owner segment already
#: closes (#979, wave-2 review).
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")

_MAX_OUTPUT_LINES = 200
_MAX_PR_LIMIT = 50
_MAX_QUERY_CHARS = 200
_MAX_HISTORY_LIMIT = 50


class ToolError(Exception):
    """A tool call was refused — bad arguments, or a tool that doesn't exist.

    The message IS the spoken refusal (spec §3.2), so write it as speech and
    make it actionable. :func:`dispatch` surfaces it as ``say`` with
    ``must_speak``; a refusal the owner never hears is the one unacceptable
    failure mode, because they are not looking at a screen and will simply
    repeat themselves into a system that already said no.
    """


#: Spoken when a well-formed `@` name is not live locally. It states the one
#: thing that was measured — nothing here answers to that name — and offers the
#: remote case as a possibility, never as a diagnosis. Asserting "that's remote"
#: about a name this layer cannot classify is how a correct transcription of a
#: real local session became an unanswerable refusal.
UNREACHABLE_AT_REFUSAL = (
    "There's no live session called '{value}' on this machine. If it's running "
    "somewhere else, I can't reach other machines yet. If it's local, check "
    "fleet_sessions and say the name again."
)


def _session_arg(args: dict, key: str = "session") -> str:
    value = args.get(key)
    if (
        not isinstance(value, str)
        or not _SESSION_RE.match(value.strip())
        or ".." in value.split("/")  # belt-and-braces; the pattern already refuses it
    ):
        # SHAPE first, always. A garbled name that happens to contain an `@`
        # ("we b at x") is a mis-transcription, not a remote target, and
        # answering it with the reachability message diagnoses the wrong
        # problem — the owner is told to check another machine when what they
        # need is to say the name again.
        raise ToolError(
            f"'{value}' is not a valid session name. Ask which session was meant, "
            "then use the exact name from fleet_sessions."
        )
    session = value.strip()
    if "@" in session and not _is_live_locally(session):
        raise ToolError(UNREACHABLE_AT_REFUSAL.format(value=strip_controls(session)[:60]))
    return session


def _is_live_locally(session: str) -> bool:
    """Does local tmux report *session*, by its WHOLE name?

    Only POSITIVE knowledge decides, matching ``write_tools._require_live``
    (spec §5): ``live_sessions()`` returns None when tmux itself is
    unreachable, which is an outage rather than a verdict, and refusing there
    would ground every local ``@`` name during a tmux blip. The CLI reports
    what it finds instead.
    """
    from .. import inbox

    live = inbox.live_sessions()
    return True if live is None else session in live


def _int_arg(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = args.get(key, default)
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


#: The one shared schema for tools whose only argument is a session name.
_SESSION_PARAM = {
    "type": "object",
    "properties": {
        "session": {
            "type": "string",
            "description": "Exact session name, as reported by fleet_sessions.",
        },
    },
    "required": ["session"],
    "additionalProperties": False,
}


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
    """Live sessions, with infrastructure services suppressed by default (#1038).

    Spoken lists pay per word, so healthy service sessions (portal, scheduler,
    stt, …) collapse to the one-line summary already in the payload; an
    UNHEALTHY service stays in the list — it IS news. Pass
    ``include_services=true`` when the owner is asking about the services
    themselves.
    """
    data = run_agentwire_cmd(["list", "--sessions"])
    if not data.get("success") or args.get("include_services") is True:
        return data
    data["sessions"] = [
        s for s in data.get("sessions", [])
        if not s.get("service") or s.get("service_healthy") is False
    ]
    return data


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


def _query_arg(args: dict, key: str = "query") -> str:
    """A free-text search string, made safe as a positional CLI argument.

    Model-supplied free text is the one class of value that cannot be
    pattern-anchored, so it gets the freeze discipline instead: controls
    stripped, length-bounded, and leading dashes removed so it can never reach
    the CLI as a flag (the twice-shipped bug — see ``_SESSION_RE``'s comment).
    """
    value = args.get(key)
    text = strip_controls(value).strip() if isinstance(value, str) else ""
    text = text.lstrip("-").strip()
    if not text:
        raise ToolError("I need a few words to search for. Say what to look up.")
    return text[:_MAX_QUERY_CHARS]


def _fleet_session_info(args: dict) -> dict:
    return run_agentwire_cmd(["info", "-s", _session_arg(args)])


def _fleet_scheduler_status(args: dict) -> dict:
    return run_agentwire_cmd(["scheduler", "status"])


def _fleet_scheduler_history(args: dict) -> dict:
    return run_agentwire_cmd(["scheduler", "history", "--json"])


def _fleet_scheduler_live(args: dict) -> dict:
    return run_agentwire_cmd(["scheduler", "live", "--json"])


def _fleet_tasks(args: dict) -> dict:
    argv = ["task", "list"]
    if args.get("session") is not None:
        argv.append(_session_arg(args))
    return run_agentwire_cmd(argv)


def _fleet_machines(args: dict) -> dict:
    return run_agentwire_cmd(["machine", "list"])


def _fleet_services(args: dict) -> dict:
    return run_agentwire_cmd(["services", "status"])


def _fleet_history(args: dict) -> dict:
    limit = _int_arg(args, "limit", 20, 1, _MAX_HISTORY_LIMIT)
    return run_agentwire_cmd(["history", "list", "-n", str(limit)])


def _fleet_locks(args: dict) -> dict:
    return run_agentwire_cmd(["lock", "list"])


def _fleet_portal(args: dict) -> dict:
    return run_agentwire_cmd(["portal", "status"])


def _fleet_councils(args: dict) -> dict:
    return run_agentwire_cmd(["council", "list"])


def _fleet_wiki_search(args: dict) -> dict:
    return run_agentwire_cmd(["wiki", "query", _query_arg(args)])


def _fleet_session_inbox(args: dict) -> dict:
    return run_agentwire_cmd(["msg", "inbox", "-s", _session_arg(args)])


def _fleet_roles(args: dict) -> dict:
    return run_agentwire_cmd(["roles", "list"])


def _fleet_network(args: dict) -> dict:
    return run_agentwire_cmd(["network", "status"], json_output=False, timeout=60)


_MAX_ACTIVITY_LIMIT = 100
_MAX_ACTIVITY_HOURS = 72


def _fleet_activity(args: dict) -> dict:
    """The fleet's activity ledger — awareness the buddy PULLS (#1016).

    Deliberately a read and not a push: everything in the buddy's spool gets
    spoken eventually, so routing ordinary lifecycle churn there would turn the
    buddy into a narrator. The short list that DOES earn a spoken mention
    arrives as ordinary mail through ``buddy_inbox``; this is everything else,
    waiting to be asked about.
    """
    from .. import fleet_activity as _activity

    argv = [
        "activity", "list",
        "--limit", str(_int_arg(args, "limit", 25, 1, _MAX_ACTIVITY_LIMIT)),
        "--hours", str(_int_arg(args, "hours", 12, 1, _MAX_ACTIVITY_HOURS)),
    ]
    event = args.get("event")
    if event is not None:
        # Checked against the ledger's own vocabulary rather than passed
        # through: `--event` is an argparse `choices` field, so a mis-heard
        # value would exit(2) with a usage message the buddy would then read
        # out as if it were an answer.
        if not isinstance(event, str) or event not in _activity.EVENTS:
            raise ToolError(
                f"I don't track an activity kind called '{event}'. The kinds are: "
                + ", ".join(_activity.EVENTS).replace("_", " ")
            )
        argv += ["--event", event]
    if args.get("session") is not None:
        argv += ["-s", _session_arg(args)]
    return run_agentwire_cmd(argv)


def _fleet_voice_health(args: dict) -> dict:
    return {
        "success": True,
        "tts": run_agentwire_cmd(["tts", "status"]),
        "stt": run_agentwire_cmd(["stt", "status"]),
    }


#: How old spooled mail may be and still be VOLUNTEERED (#1048). Speech has no
#: scrollback: an hours-stale "is idle and done working" about workers torn
#: down the previous night was spoken as fresh news. Past this, the notice is
#: dropped with a reason rather than read out — the spool keeps the body, and
#: the ack of a later spoken notice retires it.
STALE_NOTICE_S = 900

#: Grace before "sender session gone" makes a message stale. A worker that
#: reports and is reaped seconds later still deserves its report spoken; one
#: whose sender has been gone for minutes is history, not news.
SENDER_GONE_GRACE_S = 300


def _staleness_reason(m: dict, now_ms: int, live: "set | None") -> "str | None":
    """Why *m* must not be volunteered, or None while it is still news.

    Verified against CURRENT state at read time, never replayed from what was
    true at enqueue (#1048): a prompt alert whose marker is gone or answered,
    an activity notice whose subject session no longer exists, mail from a
    long-gone sender, and anything past :data:`STALE_NOTICE_S`. *live* is the
    tmux session set, or None when tmux itself was unreachable — an outage is
    not a verdict, so gone-checks abstain then (only the age gate still runs).
    """
    from .. import fleet_alerts

    age_s = max(0, now_ms - int(m.get("ts") or 0)) / 1000.0
    ref = str(m.get("ref") or "")
    if ref.startswith("prompt:"):
        # prompt:{session}:{pane}:{hash} — session names never contain `:`
        # (tmux rewrites it to `_`, #878), so a plain rsplit is exact.
        parts = ref[len("prompt:"):].rsplit(":", 2)
        if len(parts) == 3:
            from .. import prompt_router

            session, pane_s, phash = parts
            try:
                marker = prompt_router.read_marker(session, int(pane_s))
            except Exception:
                marker = None
            if not marker or str(marker.get("hash") or "") != phash:
                return "prompt no longer live"
    if ref.startswith("activity:session_idle:") and live is not None:
        subject = ref[len("activity:session_idle:"):]
        if subject and subject not in live:
            return "subject session gone"
    if age_s > STALE_NOTICE_S:
        return f"older than {STALE_NOTICE_S // 60} minutes"
    sender = str(m.get("from") or "")
    if (
        sender
        and sender not in fleet_alerts.MACHINE_SENDERS
        and live is not None
        and sender not in live
        and age_s > SENDER_GONE_GRACE_S
    ):
        return "sender session gone"
    return None


def _gate_notices(messages: list[dict]) -> "tuple[list[dict], list[dict]]":
    """The volunteering gate (#1048): (still news, dropped-with-reason).

    Two passes. Staleness first — each message verified against current state.
    Then identity dedup: messages sharing a non-empty ``ref`` are re-raises of
    one condition (the no-parent detector re-fires per TTL with a fresh id),
    and only the NEWEST speaks; the rest are superseded. Dropped entries carry
    ``{id, from, ref, reason}`` so the client can put the reason on screen —
    dropped, never silently missing.
    """
    import time

    from .. import inbox

    now_ms = int(time.time() * 1000)
    live = inbox.live_sessions()
    live_set = set(live) if live is not None else None

    dropped: list[dict] = []
    fresh: list[dict] = []
    for m in messages:
        reason = _staleness_reason(m, now_ms, live_set)
        if reason:
            dropped.append(
                {"id": m.get("id"), "from": m.get("from"),
                 "ref": m.get("ref"), "reason": reason}
            )
        else:
            fresh.append(m)

    newest_by_ref: dict[str, str] = {}
    for m in fresh:
        ref = str(m.get("ref") or "")
        if ref:
            newest_by_ref[ref] = m.get("id")
    kept: list[dict] = []
    for m in fresh:
        ref = str(m.get("ref") or "")
        if ref and newest_by_ref.get(ref) != m.get("id"):
            dropped.append(
                {"id": m.get("id"), "from": m.get("from"),
                 "ref": ref, "reason": "superseded by a newer re-raise"}
            )
        else:
            kept.append(m)
    return kept, dropped


def _buddy_inbox(args: dict) -> dict:
    """The buddy's OWN mail — what other sessions have reported to it.

    Reads the spool the delivery adapter writes. The cursor advances only on an
    ack, so the buddy marks mail read once it has actually said it out loud — an
    unacked read after a dropped call is re-read, not lost.

    ``ack_through`` is the one to reach for (#970): it acks EXACTLY the message
    named, so anything that landed between the read and the ack is still pending
    by construction. ``ack`` sweeps to the tail as it stands at ack time, which
    is not what the caller read. ``acked`` reports whether the cursor actually
    moved — a refused ack that reads as success re-announces forever.
    """
    name = args.get("_buddy") or ""
    if not name:
        raise ToolError("buddy identity missing from tool context")
    # PRESENCE, not truth. Keying the precedence on the stripped value collapses
    # "no ack_through" with "an ack_through that stripped to nothing", so
    # {ack: true, ack_through: ""} — or null, or whitespace — fell through to the
    # bool path and swept the tail: the exact loss this parameter exists to
    # close, reachable from inside its own guard. Asking to ack through nothing
    # acks nothing and says so; re-reading is the cheap failure, and a sweep is
    # the one that has no screen to surface it.
    asked_through = "ack_through" in args
    through = args.get("ack_through")
    if through is not None and not isinstance(through, str):
        raise ToolError("ack_through must be a message id")
    # Not stripped: an id comes from a prior read of this same spool, never a
    # human's fingers, so " m1 " is a caller that has already lost track of what
    # it read. Refusing is the loud failure; trimming it into a match is a guess
    # on the one operation where guessing generously loses mail.
    through = through or ""
    ack = bool(args.get("ack", False))
    unread_only = bool(args.get("unread_only", True))
    messages = delivery.read_spool(
        name, unread_only=unread_only, ack=ack and not asked_through
    )
    acked = (
        delivery.advance_cursor(name, through)
        if asked_through
        else bool(ack and messages)
    )
    # The staleness gate runs on the UNREAD view only (#1048) — that is the
    # volunteering path, and a stale notice must be dropped at speak time with
    # a reason, never replayed as fresh news. A deliberate full-history read
    # (unread_only=false) is the owner asking for the record and gets it
    # unfiltered. Dropped messages stay in the spool; the contiguous ack of a
    # later spoken notice retires them from the unread view for good.
    dropped: list[dict] = []
    if unread_only:
        from .. import fleet_alerts

        messages, dropped = _gate_notices(messages)
        for d in dropped:
            fleet_alerts.log_event("stale_notice_dropped", buddy=name, **d)
    return {
        "success": True,
        "acked": acked,
        "acked_through": through,
        "count": len(messages),
        "messages": [
            {k: m.get(k) for k in ("id", "from", "kind", "text", "ts", "ref")}
            for m in messages
        ],
        "dropped": dropped,
    }


_MAX_SENT_LIMIT = 50


def _buddy_sent(args: dict) -> dict:
    """The buddy's OWN writes — what it has actually sent, verbatim (#958).

    Reads the outbox the confirm spine appends to on every executed write, so
    the ``body`` field here is the exact rendered string that went out — not a
    description of it, and not a re-render. Delivery state is looked up live
    against the recipient's inbox on every call, because it changes after the
    write returns.
    """
    name = args.get("_buddy") or ""
    if not name:
        raise ToolError("buddy identity missing from tool context")
    limit = _int_arg(args, "limit", 10, 1, _MAX_SENT_LIMIT)
    proposal_id = args.get("proposal_id")
    entries = outbox.read_outbox(name)
    if isinstance(proposal_id, str) and proposal_id.strip():
        wanted = proposal_id.strip()
        entries = [e for e in entries if e.get("proposal_id") == wanted]
    entries = entries[:limit]
    return {
        "success": True,
        "count": len(entries),
        "sent": [
            {
                "proposal_id": e.get("proposal_id", ""),
                "session": e.get("session", ""),
                "body": e.get("body", ""),
                "instruction": e.get("instruction", ""),
                "argv": e.get("argv", []),
                "ts": e.get("ts", 0),
                "delivery": outbox.delivery_state(e),
            }
            for e in entries
        ],
    }


READ_ONLY_TOOLS: tuple[ReadOnlyTool, ...] = (
    ReadOnlyTool(
        name="fleet_sessions",
        description=(
            "List every live agentwire session with its role, parent and activity. "
            "This is the answer to 'what is running' and the source of truth for "
            "exact session names. Infrastructure services (portal, scheduler, "
            "stt, …) are collapsed into the 'services' summary line unless one "
            "is unhealthy — pass include_services=true only when the owner asks "
            "about the services themselves."
        ),
        run=_fleet_sessions,
        parameters={
            "type": "object",
            "properties": {
                "include_services": {
                    "type": "boolean",
                    "description": (
                        "List healthy infrastructure service sessions inline "
                        "instead of collapsing them into the summary."
                    ),
                },
            },
        },
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
            "this when asked what needs attention. Mark mail read only once you have "
            "actually told the owner what it says, and mark it with ack_through set to "
            "the id of the last message you said out loud — ack sweeps past anything "
            "that arrived while you were speaking, and that mail is then never read "
            "by anyone."
        ),
        run=_buddy_inbox,
        parameters={
            "type": "object",
            "properties": {
                "ack_through": {
                    "type": "string",
                    "description": (
                        "Id of the last message you actually said out loud. Marks "
                        "everything up to and including it read, and nothing after."
                    ),
                },
                "ack": {
                    "type": "boolean",
                    "description": (
                        "Mark ALL unread messages read, including any that arrived "
                        "since you read. Prefer ack_through. Default false."
                    ),
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "Only unread messages. Default true.",
                },
            },
            "additionalProperties": False,
        },
    ),
    ReadOnlyTool(
        name="buddy_sent",
        description=(
            "Messages YOU have sent, newest first: the exact body that went out, "
            "who it went to, and its current delivery state. Quote the body from "
            "here; never answer from memory or by reading the recipient's "
            "terminal. The states, and exactly what each one licenses you to "
            "say: 'queued' — still waiting in their inbox, not read yet; "
            "'dead_lettered' — it failed and was dropped, say so and say why "
            "(the reason is in detail); 'dispatch_failed' — it never went out "
            "at all; 'executed' — the command ran and carried no message body; "
            "'no_longer_queued' — it has left their queue, which is what "
            "delivery looks like AND what a purge looks like, so say it is no "
            "longer waiting and that you cannot confirm they read it; "
            "'unknown' — you could not check. Never upgrade one of these to "
            "'delivered'; that word claims more than anything here establishes."
        ),
        run=_buddy_sent,
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"How many recent sends to return (1-{_MAX_SENT_LIMIT}, default 10).",
                },
                "proposal_id": {
                    "type": "string",
                    "description": "Only the send with this proposal id.",
                },
            },
            "additionalProperties": False,
        },
    ),
    ReadOnlyTool(
        name="fleet_session_info",
        description=(
            "Details of ONE session: working directory, panes, posture, roles. "
            "Use the exact name from fleet_sessions."
        ),
        run=_fleet_session_info,
        parameters=_SESSION_PARAM,
    ),
    ReadOnlyTool(
        name="fleet_scheduler_status",
        description="Scheduler health: enabled/disabled, what is currently dispatching.",
        run=_fleet_scheduler_status,
    ),
    ReadOnlyTool(
        name="fleet_scheduler_history",
        description="Recent scheduled-task runs and how they ended.",
        run=_fleet_scheduler_history,
    ),
    ReadOnlyTool(
        name="fleet_scheduler_live",
        description="Scheduled tasks currently running, with their sessions.",
        run=_fleet_scheduler_live,
    ),
    ReadOnlyTool(
        name="fleet_tasks",
        description=(
            "Named tasks configured for a session's project. Without a session, "
            "lists tasks for the current project context."
        ),
        run=_fleet_tasks,
        parameters={
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "Exact session name, as reported by fleet_sessions.",
                },
            },
            "additionalProperties": False,
        },
    ),
    ReadOnlyTool(
        name="fleet_machines",
        description="Registered remote machines and how to reach them.",
        run=_fleet_machines,
    ),
    ReadOnlyTool(
        name="fleet_services",
        description="Configured services and whether each is up.",
        run=_fleet_services,
    ),
    ReadOnlyTool(
        name="fleet_history",
        description="Recent past conversations across sessions, newest first.",
        run=_fleet_history,
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"How many to return (1-{_MAX_HISTORY_LIMIT}, default 20).",
                },
            },
            "additionalProperties": False,
        },
    ),
    ReadOnlyTool(
        name="fleet_locks",
        description="Active session locks — who is holding what.",
        run=_fleet_locks,
    ),
    ReadOnlyTool(
        name="fleet_portal",
        description="Portal server status: running, ports, connected clients.",
        run=_fleet_portal,
    ),
    ReadOnlyTool(
        name="fleet_councils",
        description="Council sittings that exist and whether each is live.",
        run=_fleet_councils,
    ),
    ReadOnlyTool(
        name="fleet_wiki_search",
        description=(
            "Search the knowledge wiki for past investigations and gotchas. "
            "Returns ranked page paths with snippets."
        ),
        run=_fleet_wiki_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A few words to search for.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ReadOnlyTool(
        name="fleet_session_inbox",
        description=(
            "Peek ANOTHER session's pending messages without consuming them — "
            "what is queued for it that it has not seen yet. Your own mail is "
            "buddy_inbox, not this."
        ),
        run=_fleet_session_inbox,
        parameters=_SESSION_PARAM,
    ),
    ReadOnlyTool(
        name="fleet_roles",
        description="Available session roles and what each one is for.",
        run=_fleet_roles,
    ),
    ReadOnlyTool(
        name="fleet_network",
        description="Network reachability of the portal and registered machines.",
        run=_fleet_network,
    ),
    ReadOnlyTool(
        name="fleet_activity",
        description=(
            "What the fleet has BEEN DOING recently, newest first: sessions going "
            "idle, scheduled tasks finishing, toasts shown to the owner, sessions "
            "starting and closing, and everything spoken aloud through the fleet's "
            "own text-to-speech. Use it when asked what's been happening, what you "
            "missed, or whether something already ran. Two rules about it. First, "
            "an entry marked 'spoke' was ALREADY SAID OUT LOUD to the owner by a "
            "session — they heard it, so never repeat one back as news; refer to it "
            "("
            "\"you already heard about the build\") or use it to avoid saying the "
            "same thing twice. Second, nothing here was put in front of you — it is "
            "a record you looked up, so it is only ever an answer to a question, "
            "never something you volunteer."
        ),
        run=_fleet_activity,
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"How many entries (1-{_MAX_ACTIVITY_LIMIT}, default 25).",
                },
                "hours": {
                    "type": "integer",
                    "description": f"How far back to look (1-{_MAX_ACTIVITY_HOURS}, default 12).",
                },
                "event": {
                    "type": "string",
                    "description": (
                        "Only one kind: session_idle, task_completed, toast_high, "
                        "toast, spoke, session_created, session_closed, pane_died."
                    ),
                },
                "session": {
                    "type": "string",
                    "description": "Only this session, exact name from fleet_sessions.",
                },
            },
            "additionalProperties": False,
        },
    ),
    ReadOnlyTool(
        name="fleet_voice_health",
        description="Health of the voice pipeline itself: TTS and STT backends.",
        run=_fleet_voice_health,
    ),
)

TOOLS_BY_NAME = {t.name: t for t in READ_ONLY_TOOLS}


# ---------------------------------------------------------------------------
# Write tools — same allowlist shape, one extra requirement
# ---------------------------------------------------------------------------
#
# ``write_tools`` imports from here (for ``ToolError`` and ``_session_arg``), so
# the import back the other way is deferred to call time. One direction at
# module level keeps the cycle from existing at all.


@dataclass(frozen=True)
class WriteTool:
    """A tool that can write, and therefore needs the confirm spine.

    Structurally distinct from :class:`ReadOnlyTool` rather than a flag on it:
    ``run`` here takes the spine as a second argument, so a write tool
    physically cannot be invoked by a caller that has no gate to hand it. A
    boolean would have let one through by omission.
    """

    name: str
    description: str
    run: Callable[[dict, object], dict]
    parameters: dict

    def to_realtime_tool(self) -> dict:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def write_tools() -> "tuple[WriteTool, ...]":
    from . import write_tools as _write_tools

    return tuple(
        WriteTool(name=name, description=description, parameters=parameters, run=run)
        for name, description, parameters, run in _write_tools.WRITE_TOOL_SPECS
    )


def all_tools() -> "tuple[object, ...]":
    """Every tool the model may call: the read allowlist plus the gated writes."""
    return (*READ_ONLY_TOOLS, *write_tools())


def realtime_tool_defs() -> list[dict]:
    """The full ``session.tools[]`` array for a Realtime session."""
    return [t.to_realtime_tool() for t in all_tools()]


def dispatch(name: str, args: dict, buddy: str, spine=None) -> dict:
    """Run one tool call by name. Never raises — errors come back as data.

    Returning an error rather than raising is what lets the buddy *say* what
    went wrong ("I don't have a session by that name — which one did you
    mean?") instead of the conversation stalling on an unresolved function
    call.

    *spine* is the :class:`~agentwire.voice_layer.confirm.ConfirmSpine`. Without
    one, write tools are refused outright rather than silently degraded to an
    ungated write — a caller that forgot to wire the gate must fail loudly.

    **Every refusal here speaks** (spec §3.2). Each error path returns ``say``
    plus ``must_speak``, so there is no route by which a refusal reaches the
    model as something it can quietly swallow and retry around. The owner is not
    watching a screen; an unspoken refusal is indistinguishable from the buddy
    having simply not heard them.
    """
    payload = dict(args or {})
    payload["_buddy"] = buddy

    def spoken_error(message: str, reason: str) -> dict:
        return {
            "success": False,
            "reason": reason,
            "error": message,
            "say": message,
            "must_speak": True,
        }

    def guarded(run):
        try:
            return run()
        except ToolError as exc:
            return spoken_error(str(exc), "refused")
        except Exception as exc:  # a tool failure must never kill the conversation
            # Deliberately not re-raised and deliberately not silent: an
            # unexpected failure the owner never hears about is the same
            # experience as the buddy ignoring them.
            return spoken_error(
                f"Something went wrong running {name}, so nothing happened: {exc}",
                "tool_failed",
            )

    tool = TOOLS_BY_NAME.get(name)
    if tool is not None:
        return guarded(lambda: tool.run(payload))

    writes = {t.name: t for t in write_tools()}
    write_tool = writes.get(name)
    if write_tool is None:
        available = ", ".join(sorted({*TOOLS_BY_NAME, *writes}))
        return spoken_error(
            f"I don't have a tool called {name}, so I did nothing. "
            f"Available: {available}",
            "no_such_tool",
        )
    if spine is None:
        return spoken_error(
            f"{name} needs the confirm gate and this dispatcher has none. "
            "Nothing was sent.",
            "no_confirm_gate",
        )
    return guarded(lambda: write_tool.run(payload, spine))
