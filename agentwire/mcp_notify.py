"""MCP tools — notify domain."""

from .core import run_agentwire_cmd
from .mcp_core import (
    mcp,
)


@mcp.tool()
def notify_parent(text: str, session: str | None = None) -> str:
    """Notify your PARENT/orchestrator session — text injected into their prompt.

    Up-the-hierarchy report: status, completion, escalation. One of the notify_*
    family — see also notify_user (human desktop toast) and notify_event (portal
    lifecycle events).

    Args:
        text: Notification message.
        session: Target session (optional; defaults to your parent from .agentwire.yml).

    Returns:
        Success message or error description.
    """
    args = ["notify-parent"]
    if session:
        args.extend(["--to", session])
    args.append(text)

    data = run_agentwire_cmd(args)
    target = data.get("target", session or "parent")
    if data.get("delivered"):
        return f"Notification delivered to {target} (verified)."
    reason = data.get("reason") or data.get("error") or "unknown reason"
    return f"Notification NOT delivered to {target}: {reason}"


@mcp.tool()
def notify_event(event: str, session: str | None = None) -> str:
    """Broadcast a portal LIFECYCLE event (session/pane state change) to the dashboard.

    System/infra signal — usually emitted by tmux hooks, not by hand. One of the
    notify_* family — see also notify_parent (your orchestrator) and notify_user
    (human desktop toast).

    Args:
        event: Event type (e.g., 'session_idle', 'session_active').
        session: Session name (optional, auto-detected if in tmux).

    Returns:
        Success message or error description.
    """
    args = ["notify-event", event]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to broadcast event: {data.get('error', 'Unknown error')}"
    # Lifecycle events are ephemeral — report whether any dashboard saw it (#444).
    n = data.get("clients", 0)
    if n > 0:
        return f"Event '{event}' broadcast to {n} dashboard client{'s' if n != 1 else ''}."
    return f"Event '{event}' had no listeners — no dashboard connected (nothing saw it)."


@mcp.tool()
def notify_user(text: str, session: str | None = None, priority: str = "normal",
                artifact_url: str | None = None, artifact_title: str | None = None) -> str:
    """Show the HUMAN a desktop toast on the portal (persistent, visual).

    The human-screen channel — the asymmetric text partner to `say` (audio).
    Supports a safe markdown subset (bold, line breaks, [links](url)). One of the
    notify_* family — see also notify_parent (your orchestrator) and notify_event
    (portal lifecycle). Clicking the toast opens the session that generated the
    notification (the `session` below); a toast with no session is non-clickable.

    With `artifact_url` set, the toast instead becomes a click-to-open artifact
    notice (#817): it sticks until dismissed, also appears in the Session HUD,
    and clicking it opens that artifact window — use this to hand the human a
    rendered deliverable without stealing focus.

    Args:
        text: Notification text. Bold (**x**), line breaks, and [links](https://…)
            render; everything else is escaped.
        session: Session this relates to (shown as a badge).
        priority: 'normal' or 'high' (high gets an accent border).
        artifact_url: URL or ~/.agentwire/artifacts/ filename to open on click.
        artifact_title: Window title for the artifact (default: "Artifact").

    Returns:
        Notification ID or error description.
    """
    # Routed through core's shared toast call rather than posting here (#1016).
    # This tool is the one agents actually reach — CLAUDE.md's rule is MCP for
    # agents, CLI for humans — so a producer that posts on its own transport is
    # precisely the one a CLI-side hook cannot see, and agent-generated toasts
    # were invisible to the fleet's awareness ledger. One seam, below every
    # producer.
    from .core import post_desktop_notification

    data = post_desktop_notification(
        text,
        session=session,
        priority=priority,
        artifact=({"url": artifact_url, "title": artifact_title or "Artifact"}
                  if artifact_url else None),
    )
    if not data.get("success"):
        return f"Failed to post notification: {data.get('error', 'Unknown error')}"
    # Honest delivery: how many dashboards saw it live (#444). The toast is
    # persisted and restored on next load, so 0 clients ≠ lost.
    n = data.get("clients", 0)
    if n > 0:
        return f"Toast shown to {n} connected client{'s' if n != 1 else ''} (id: {data.get('id')})."
    return (f"Toast queued (id: {data.get('id')}) — no portal open right now; "
            f"it will appear on next load.")
