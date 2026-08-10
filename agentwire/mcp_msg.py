"""MCP tools — msg domain."""

from .mcp_core import (
    get_caller_session,
    mcp,
    run_agentwire_cmd,
)


@mcp.tool()
def msg_send(to: str, text: str, kind: str = "note", ref: str = "") -> str:
    """Send a POLITE, non-interrupting message to another session's inbox.

    Use this for routine peer updates that should NOT interrupt — a worker
    reporting "PR drafted", an orchestrator nudging a sibling. The message
    drops into a durable inbox and only injects when the recipient's input box
    is empty and the pane is safe, so it can never clobber a human who is
    mid-typing. Delivery is at the next safe boundary (≤60s), not instant.

    Sending to a named session that doesn't currently exist still queues (the
    session may be about to be created), but the confirmation carries a
    warning: unless the session appears, the message dead-letters within a few
    minutes (and load-bearing kinds email the owner). Heed the warning — a
    gone recipient usually means a stale session name; check sessions_list.

    Prefer `session_send` ONLY when you must forcibly drive a session right
    now (it pastes + Enter immediately, overwriting any uncommitted draft).

    Args:
        to: Recipient session name, or "@all" to broadcast to every live
            agent session except yourself.
        text: The message body.
        kind: One of note (default), done, request, escalation, ingest, voice.
            `ingest` is PASSIVE — never auto-delivered, so it never drives the
            recipient into a turn; it waits until they `msg_pull` it. Use it for
            "output ready to ingest" awareness signals (Briefing Mode): drop a
            passive pointer to a file the recipient reads on the human's cue.
            `voice` is the owner speaking through their voice buddy — active
            (it drives the recipient like a `request`) and escalatable, but NOT
            an interrupt; `escalation` remains the only kind that pre-empts.
            The buddy sets it; don't hand-pick it for ordinary agent mail.
        ref: Optional machine-readable pointer (e.g. a report file path),
            surfaced as a typed field on the message — pair with kind="ingest"
            so the recipient can open the file without parsing free text.

    Returns:
        Confirmation of which sessions were queued — plus a warning when a
        named recipient doesn't currently exist — or an error.
    """
    caller = get_caller_session()
    args = ["msg", "send", "--to", to, "--kind", kind]
    if caller:
        args += ["--from", caller]
    if ref:
        args += ["--ref", ref]
    args.append(text)
    data = run_agentwire_cmd(args)
    if data.get("success"):
        recipients = data.get("recipients") or []
        if not recipients:
            return f"No live recipients for '{to}'."
        out = f"Queued {kind} → {', '.join(recipients)} (delivers when their box is clear)."
        for warning in data.get("warnings") or []:
            out += f"\nWarning: {warning}"
        return out
    return f"Failed to queue message: {data.get('error', 'Unknown error')}"


@mcp.tool()
def msg_inbox(session: str | None = None) -> str:
    """Peek a session's pending + passive messages (does not drain or consume).

    Shows both the driving `pending` messages (auto-delivered when the box
    clears) and the `passive` ingest messages (which wait until you `msg_pull`).

    Args:
        session: Session name (default: the calling session).

    Returns:
        The pending and passive messages, or a note that the inbox is empty.
    """
    args = ["msg", "inbox"]
    if session:
        args += ["-s", session]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to read inbox: {data.get('error', 'Unknown error')}"
    pending = data.get("pending") or []
    passive = data.get("passive") or []
    if not pending and not passive:
        return f"Inbox empty for {data.get('session', session or 'this session')}."
    lines = []
    if pending:
        lines.append(f"{len(pending)} pending for {data.get('session')}:")
        for m in pending:
            lines.append(f"  [{m.get('kind')}] from {m.get('from')}: {m.get('text')}")
    if passive:
        lines.append(f"{len(passive)} passive (ingest) — call msg_pull to consume:")
        for m in passive:
            lines.append(f"  [{m.get('kind')}] from {m.get('from')}: {m.get('text')}")
            if m.get('ref'):
                lines.append(f"      ref: {m.get('ref')}")
    return "\n".join(lines)


@mcp.tool()
def msg_pull(session: str | None = None) -> str:
    """Read and REMOVE passive (ingest) awareness messages — the voluntary pull.

    This is the Briefing Mode anchor's move: ingest messages are never pushed to
    you, so call this on the human's cue ("what's ready?") to collect the
    "output ready" pointers correspondents dropped. Pulling consumes them; the
    actual content lives in the files they point at, which you then read.

    Args:
        session: Session name (default: the calling session).

    Returns:
        The pulled messages, or a note that there were none.
    """
    args = ["msg", "pull"]
    if session:
        args += ["-s", session]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to pull messages: {data.get('error', 'Unknown error')}"
    pulled = data.get("pulled") or []
    if not pulled:
        return f"No passive (ingest) messages for {data.get('session', session or 'this session')}."
    lines = [f"Pulled {len(pulled)} passive message(s):"]
    for m in pulled:
        lines.append(f"  [{m.get('kind')}] from {m.get('from')}: {m.get('text')}")
        if m.get('ref'):
            lines.append(f"      ref: {m.get('ref')}")
    return "\n".join(lines)


@mcp.tool()
def research_dir(session: str | None = None) -> str:
    """Resolve (and create) the Briefing Mode research dropbox for a session.

    Returns the blessed path under ~/.agentwire/research/<session>/ where an
    anchor's correspondents file their reports. The anchor passes this path to
    each correspondent and reads the files there when pulling ingest pointers.

    Args:
        session: Anchor session name (default: the calling session).

    Returns:
        The dropbox path (created if missing), or an error.
    """
    args = ["research", "ensure"]
    if session:
        args += ["-s", session]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to resolve research dir: {data.get('error', 'Unknown error')}"
    return f"Research dropbox: {data.get('path')}"


@mcp.tool()
def msg_dead(session: str | None = None) -> str:
    """List dead-lettered polite messages — ones dropped after retrying out.

    A `msg` whose recipient never cleared its input box (or stayed parked /
    non-agent) is retried for ~40 minutes; one whose recipient doesn't exist
    dead-letters after only a few minutes (`target_gone`). Either way the drop
    is recorded here rather than lost silently. Use this to see what never
    reached someone, and why.

    Args:
        session: Scope to one session's graveyard. Omitted means GLOBAL —
            every session that has dead-lettered messages (#693: it never
            defaults to the calling session, so a monitoring loop inside a
            session still sees the whole graveyard).

    Returns:
        The dead-lettered messages with their drop reason + timestamp, or a
        note that there are none.
    """
    args = ["msg", "dead"]
    if session:
        args += ["-s", session]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to read dead letters: {data.get('error', 'Unknown error')}"
    groups = data.get("sessions") or []
    total = data.get("total", 0)
    if not total:
        scope = f" for {session}" if session else ""
        return f"No dead-lettered messages{scope}."
    lines = [f"{total} dead-lettered message(s):"]
    for g in groups:
        lines.append(f"{g.get('session')} ({len(g.get('dead') or [])}):")
        for m in g.get("dead") or []:
            lines.append(
                f"  [{m.get('kind')}] from {m.get('from')} — "
                f"{m.get('attempts')} attempts ({m.get('reason') or 'unknown'}): "
                f"{m.get('text')}"
            )
    return "\n".join(lines)


@mcp.tool()
def msg_purge(session: str | None = None) -> str:
    """Drop a session's PENDING polite messages — the self-heal escape hatch.

    Use this to un-wedge a recipient stuck re-seeing the same messages (or to
    clear an inbox you know is stale). It drops the active drain queue outright —
    no empty-box gate, no delivery. Dead-lettered (`msg_dead`) and passive
    `ingest` messages are untouched; this is strictly the pending queue.

    Args:
        session: Session whose pending queue to drop (default: the caller).

    Returns:
        How many pending messages were dropped.
    """
    caller = session or get_caller_session()
    args = ["msg", "purge"]
    if caller:
        args.append(caller)
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to purge: {data.get('error', 'Unknown error')}"
    return f"Purged {data.get('purged', 0)} pending message(s) from {data.get('session')}."


@mcp.tool()
def msg_flush(session: str | None = None, force: bool = False) -> str:
    """Attempt a polite-message drain now (gated on an empty box + safe target).

    Messages drain automatically every ≤60s via the watchdog; use this to force a
    pass without waiting. By default it does NOT bypass the safety gates — a
    busy/parked/non-agent recipient is still deferred. Passive `ingest` messages
    are never drained (pull them with msg_pull).

    Args:
        session: Session to flush (default: all sessions with queued messages).
        force: Bypass the empty-box gate and paste anyway (requires `session`;
            may land mid-draft). For un-wedging a stuck queue. Safety guards
            (gone/parked/non-agent/live-dialog) are never bypassed.

    Returns:
        What was delivered or deferred.
    """
    args = ["msg", "flush"]
    if session:
        args += ["-s", session]
    if force:
        args.append("--force")
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to flush: {data.get('error', 'Unknown error')}"
    if session:
        if data.get("delivered"):
            return f"Delivered {data['delivered']} to {session}."
        return f"Deferred {session}: {data.get('reason', 'unknown')}."
    flushed = data.get("flushed") or []
    deferred = data.get("deferred") or []
    if data.get("skipped"):
        return str(data["skipped"])
    if not flushed and not deferred:
        return "No pending messages."
    parts = [f"delivered {r['delivered']} → {r['session']}" for r in flushed]
    parts += [f"deferred {r['session']}: {r.get('reason')}" for r in deferred]
    return "; ".join(parts)
