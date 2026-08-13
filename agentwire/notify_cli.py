"""CLI for notification + artifact-window commands.

``agentwire notify-parent`` (worker→orchestrator), ``agentwire notify-user``
(human desktop toast), ``agentwire notify-event`` (portal lifecycle broadcast,
usually from tmux hooks), and ``agentwire open`` (open a URL/file as an artifact
window in the portal). Pure relocation from ``__main__`` (#495).
"""

from __future__ import annotations

import json
import sys
import urllib.request

from . import pane_manager
from .core import (
    _get_portal_url,
    _output_json,
    _output_result,
    _portal_auth_headers,
    _post_desktop_notification,
    read_body_file,
)

#: The ``notify-event`` vocabulary worth remembering (#1016). Deliberately not
#: the whole vocabulary — see :func:`cmd_notify`.
_LEDGER_EVENTS = ("session_created", "session_closed", "pane_died")


def cmd_notify_parent(args) -> int:
    """Notify parent session (worker→orchestrator communication).

    Sends a prefixed text message to the parent session via tmux.
    The parent is determined from .agentwire.yml or --to flag.

    This is for session hierarchy communication. For outbound notifications
    to the user across devices, use `agentwire email` or `agentwire quo`.

    Notification targets (in priority order):
    1. --to SESSION if specified
    2. parent from .agentwire.yml if exists
    3. pane 0 of current session (if in worker pane)

    Examples:
        agentwire notify "Worker 1 completed task"
        agentwire notify --to agentwire "Build finished"
    """
    json_mode = getattr(args, 'json', False)
    body_file = getattr(args, "body_file", None)
    if body_file is not None and args.text:
        return _output_result(
            False, json_mode,
            "--body-file and positional text are mutually exclusive")
    if body_file is not None:
        try:
            text = read_body_file(body_file)
        except OSError as exc:
            return _output_result(False, json_mode, f"--body-file: {exc}")
    else:
        text = " ".join(args.text) if args.text else ""

    if not text.strip():
        return _output_result(
            False, json_mode,
            "Usage: agentwire notify-parent <message | --body-file PATH>")

    target_session = getattr(args, 'to', None)
    current_session = pane_manager.get_current_session()
    current_pane = pane_manager.get_current_pane_index()

    # --on-idle: the idle hook is reporting that a pane-0 session went idle.
    # Infrastructure services (portal, tts/stt, scheduler, the idle-nag bridge,
    # custom services) cycle active→idle constantly and are NOT delegated work,
    # so they must never fire a "child finished" ping at their parent. A
    # worktree / `agentwire new` child is not a service and is unaffected.
    if getattr(args, 'on_idle', False) and current_session:
        from agentwire import services

        if services.is_service_session(current_session):
            return _output_result(True, json_mode, "", skipped="service-session")

    # If no explicit target, resolve the parent through the SAME precedence the
    # prompt router uses (worker pane → pane 0; else creator recorded at
    # `agentwire new` time; else `.agentwire.yml parent:`). The old path looked
    # only at `.agentwire.yml`, so a worktree/`agentwire new` child — whose
    # parent lives in session metadata, not config — resolved to nothing and its
    # idle notification silently dropped (the parent never heard the child go
    # idle). resolve_parent reads the creator metadata, closing that gap.
    if not target_session and current_session is not None and current_pane is not None:
        from agentwire import prompt_router

        resolved = prompt_router.resolve_parent(current_session, current_pane)
        if resolved:
            target_session = resolved[0]

    # Fleet awareness (#1016). An idle is the fleet's own "I finished" signal,
    # and until now the ONLY thing it reached was a parent — so a listener with
    # no parent link (the voice buddy) could not know a delegated job had
    # landed. Recorded here, ABOVE the no-target return, because a root session
    # going idle is exactly the case that returned early and told nobody.
    #
    # `exclude=target_session`: the parent hears this by paste in the lines
    # below, and the same news twice is how a channel earns being ignored.
    # Best-effort — the notify is the job, awareness is the bonus.
    if getattr(args, 'on_idle', False) and current_session and current_pane == 0:
        from agentwire import fleet_activity

        try:
            fleet_activity.note_session_idle(
                current_session, text, parent=target_session or "")
        except Exception:  # noqa: BLE001  # never break the idle path
            pass

    # Build notification message (--raw sends verbatim — queued messages
    # already carry their own [WORKER SUMMARY ...] / [PROMPT ...] headers)
    if getattr(args, 'raw', False):
        notification = text
    else:
        source = current_session or "unknown"
        if current_pane is not None and current_pane > 0:
            notification = f"[NOTIFY from {source} pane {current_pane}] {text}"
        else:
            notification = f"[NOTIFY from {source}] {text}"

    if target_session:
        if target_session == current_session and current_pane == 0:
            return _output_result(False, json_mode, "Cannot notify own pane")
    elif current_pane is not None and current_pane > 0 and current_session:
        target_session = current_session
    else:
        return _output_result(
            False, json_mode,
            "No target session (set 'parent' in .agentwire.yml or use --to)")

    # --queued (#667): route through the polite msg inbox (kind=done) instead
    # of a direct paste. The drain owns everything the direct path lacks — the
    # empty-box gate (never appends to a busy orchestrator's draft), busy
    # deferral without dead-letter penalty, full-line scrollback dedup, and
    # email-on-dead-letter — so an idle report-back can neither pile up
    # unsubmitted nor vanish silently. Non-queued callers are unchanged.
    if getattr(args, 'queued', False):
        from agentwire import inbox

        # --on-idle marks the message as the idle handler's SYNTHETIC
        # placeholder, not a report the child wrote (#952). The kind is the
        # typed discriminator the cohort ledger keys on — sentinel text is
        # defeated by any child that happens to write the same words.
        kind = "idle" if getattr(args, 'on_idle', False) else "done"
        try:
            msgs = inbox.enqueue(
                to=target_session, text=text, kind=kind,
                sender=current_session or "unknown",
            )
        except (ValueError, OSError) as e:
            if json_mode:
                _output_json({"success": False, "target": target_session,
                              "queued": False, "error": str(e)})
                return 1
            print(f"Failed to queue notification for {target_session}: {e}",
                  file=sys.stderr)
            return 1
        if json_mode:
            _output_json({"success": True, "target": target_session,
                          "queued": True, "id": msgs[0].id if msgs else None})
            return 0
        if not getattr(args, 'quiet', False):
            print(f"Queued for {target_session}")
        return 0

    # safe_deliver refuses targets where a paste could do damage (live
    # dialog on screen, bare shell, parked session) and verifies the paste
    # actually landed. Callers (queue processor) retry on failure.
    from agentwire import prompt_router

    delivered, reason = prompt_router.safe_deliver(target_session, 0, notification)
    if json_mode:
        _output_json({
            "success": delivered,
            "target": target_session,
            "delivered": delivered,
            "reason": reason if not delivered else None,
        })
        return 0 if delivered else 1
    if not delivered:
        print(f"Notification not delivered to {target_session}: {reason}", file=sys.stderr)
        return 1
    if not getattr(args, 'quiet', False):
        print(f"Notified {target_session}")
    return 0


def cmd_open(args) -> int:
    """Announce a URL or local file as a click-to-open artifact notification.

    Posts a portal notification (toast + Session HUD entry) carrying the
    artifact target instead of force-opening a window (#817) — the window
    opens, focused, only when the human clicks the notice.

    Examples:
        agentwire open dashboard.html --title "Dashboard"
        agentwire open https://example.com --title "External"
        agentwire open test.html --artifact-id my-test --json
    """
    import requests

    from .core import portal_request

    url = args.url
    title = args.title
    artifact_id = getattr(args, 'artifact_id', None)
    json_output = getattr(args, 'json', False)

    portal_url = _get_portal_url()

    artifact = {"url": url, "title": title}
    if artifact_id:
        artifact["artifact_id"] = artifact_id

    try:
        resp = portal_request(
            "POST",
            f"{portal_url}/api/desktop/notification",
            json={"artifact": artifact},
        )
        data = resp.json()

        if json_output:
            print(json.dumps(data))
        elif data.get("success"):
            print(f"Artifact announced: {title} (notification id: {data.get('id', 'unknown')}) "
                  "— click the toast or HUD entry to open")
        else:
            print(f"Failed: {data.get('error', 'Unknown error')}", file=sys.stderr)
            return 1

    except requests.exceptions.ConnectionError:
        msg = "Portal not reachable. Is it running? (agentwire portal status)"
        if json_output:
            print(json.dumps({"success": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1
    except Exception as e:
        if json_output:
            print(json.dumps({"success": False, "error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def cmd_notify_user(args) -> int:
    """Show the human a desktop toast on the portal (notify-user)."""
    text = " ".join(args.text) if args.text else ""
    json_mode = getattr(args, "json", False)
    if not text.strip():
        return _output_result(False, json_mode, "Usage: agentwire notify-user <text>")
    # Fleet awareness (#1016) rides inside _post_desktop_notification, below
    # every toast producer — see core.post_desktop_notification. Nothing to do
    # here: a per-producer hook is what leaves the next producer silent.
    ok = _post_desktop_notification(
        text, session=getattr(args, "session", None),
        priority=getattr(args, "priority", "normal"),
    )
    return _output_result(ok, json_mode,
                          "Toast posted." if ok else "Failed to post toast (portal not reachable?)")


def cmd_notify(args) -> int:
    """Send a notification to the portal about session/pane state changes.

    Called by tmux hooks to notify the portal when sessions are created/closed,
    panes are created/killed, clients attach/detach, sessions are renamed, etc.
    The portal broadcasts these events to connected dashboard clients for real-time
    UI updates.
    """
    event = args.event
    session = getattr(args, 'session', None)
    pane = getattr(args, 'pane', None)
    pane_id = getattr(args, 'pane_id', None)
    old_name = getattr(args, 'old_name', None)
    new_name = getattr(args, 'new_name', None)
    json_mode = getattr(args, 'json', False)

    if not event:
        return _output_result(False, json_mode, "Event is required")

    # Fleet awareness (#1016) — LEDGER ONLY, and only for the three events that
    # describe a session's existence. The rest of this verb's vocabulary
    # (client_attached, pane_focused, window_activity) fires on every glance at
    # a terminal; recording it would bury the events that mean something under
    # events that mean somebody moved a mouse. None of the three is ever spoken:
    # "a session started" is context for a question, not news to volunteer.
    if event in _LEDGER_EVENTS and session:
        from agentwire import fleet_activity

        try:
            fleet_activity.note_lifecycle(event, session)
        except Exception:  # noqa: BLE001  # tmux hooks must never fail loudly
            pass

    portal_url = _get_portal_url()
    if not portal_url:
        return _output_result(False, json_mode, "Portal URL not configured")

    # Build payload
    payload = {"event": event}
    if session:
        payload["session"] = session
    if pane is not None:
        payload["pane"] = pane
    if pane_id is not None:
        payload["pane_id"] = pane_id
    if old_name is not None:
        payload["old_name"] = old_name
    if new_name is not None:
        payload["new_name"] = new_name

    try:
        # Use urllib to avoid requests dependency in core CLI

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{portal_url}/api/notify",
            data=data,
            headers={"Content-Type": "application/json", **_portal_auth_headers()},
            method="POST"
        )

        # Disable SSL verification for self-signed certs
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=5, context=ctx) as response:
            result = json.loads(response.read().decode())

        if result.get("success"):
            if json_mode:
                _output_json({"success": True, "event": event, "session": session,
                              "clients": result.get("clients", 0)})
            return 0
        else:
            return _output_result(False, json_mode, result.get("error", "Unknown error"))

    except Exception as e:
        # Don't fail loudly - hooks run in background and shouldn't block tmux
        if json_mode:
            _output_json({"success": False, "error": str(e)})
        return 1


def register_notify_parser(subparsers) -> None:
    # === notify command (worker→parent) ===
    notify_cmd_parser = subparsers.add_parser("notify-parent", help="Notify parent session (worker→orchestrator)")
    notify_cmd_parser.add_argument("text", nargs="*", help="Notification message (or --body-file)")
    notify_cmd_parser.add_argument(
        "--body-file", dest="body_file", default=None, metavar="PATH",
        help="Read the message body from PATH ('-' for stdin) instead of "
             "positional text — code-bearing bodies need no shell escaping (#944)",
    )
    notify_cmd_parser.add_argument("--to", type=str, metavar="SESSION", help="Target session (default: parent from .agentwire.yml)")
    notify_cmd_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress output")
    notify_cmd_parser.add_argument("--raw", action="store_true",
                                   help="Send the message verbatim (no [NOTIFY from ...] prefix)")
    notify_cmd_parser.add_argument("--queued", action="store_true",
                                   help="Deliver via the polite msg inbox (kind=done) instead of a direct "
                                        "paste — waits for an empty input box, defers while the target is "
                                        "busy, dead-letters + emails the owner on exhaustion (#667)")
    notify_cmd_parser.add_argument("--on-idle", dest="on_idle", action="store_true",
                                   help="Idle-hook mode: suppress the notify if the current session "
                                        "is an infrastructure service (they cycle idle constantly)")
    notify_cmd_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_cmd_parser.set_defaults(func=cmd_notify_parent)

    # === open command (artifact notifications) ===
    open_parser = subparsers.add_parser("open", help="Announce a URL or local file as a click-to-open artifact notification in the portal")
    open_parser.add_argument("url", help="URL or filename to announce (filenames served from ~/.agentwire/artifacts/)")
    open_parser.add_argument("--title", "-t", type=str, default="Artifact", help="Window title")
    open_parser.add_argument("--artifact-id", type=str, help="Unique window ID for the opened artifact (auto-generated if omitted)")
    open_parser.add_argument("--json", action="store_true", help="Output JSON")
    open_parser.set_defaults(func=cmd_open)

    # === notify-event command ===
    notify_parser = subparsers.add_parser("notify-event", help="Broadcast a portal lifecycle event (session/pane state change); usually called by tmux hooks")
    notify_parser.add_argument(
        "event",
        help="Event type: session_closed, session_created, pane_died, pane_created, "
             "client_attached, client_detached, session_renamed, pane_focused, window_activity"
    )
    notify_parser.add_argument("-s", "--session", help="Session name")
    notify_parser.add_argument("--pane", type=int, help="Pane index (for pane events)")
    notify_parser.add_argument("--pane-id", help="Pane ID from tmux (for pane events via hooks)")
    notify_parser.add_argument("--old-name", help="Old session name (for session_renamed)")
    notify_parser.add_argument("--new-name", help="New session name (for session_renamed)")
    notify_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_parser.set_defaults(func=cmd_notify)

    # notify-user: human-facing desktop toast (the CLI twin of MCP notify_user)
    notify_user_parser = subparsers.add_parser("notify-user", help="Show the human a desktop toast on the portal")
    notify_user_parser.add_argument("text", nargs="+", help="Toast text (supports a safe markdown subset: bold, links, line breaks)")
    notify_user_parser.add_argument("-s", "--session", help="Session this relates to (shown as a badge)")
    notify_user_parser.add_argument("--priority", default="normal", choices=["normal", "high"], help="Toast priority")
    notify_user_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_user_parser.set_defaults(func=cmd_notify_user)
