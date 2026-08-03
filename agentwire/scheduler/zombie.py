"""Reap bare-shell scheduler sessions (#739).

``_dispatch_worktree_task`` names every worktree branch
``scheduler-<task>-<ts>``. If the session's launch crashes before the agent
starts (e.g. the worktree directory went missing between ``agentwire new``
reporting success and the pane's ``cd``), the tmux session drops to a bare
shell — which the idle-reaper correctly never touches, since it only reaps a
*running* agent that goes idle. Nothing else would ever clean that up, so it
lingers indefinitely. This module finds and kills those sessions.

Detected by branch naming rather than ``worktree_registry``. Scheduler
dispatch does register now (#837 routed ``agentwire new``'s worktree
creation through the shared create+register helper), but registration is not
the right signal here: the zombie case is precisely a session whose worktree
DIRECTORY went missing after creation, and the tmux+branch-name scan finds
it without depending on any bookkeeping surviving the crash.
"""

import subprocess
import time

from ..worktree import parse_session_name

BRANCH_PREFIX = "scheduler-"

# tmux `session_created` is whole seconds. A healthy launch's pre-agent shell
# moment lasts well under a second (see `_launch_tmux_session`'s 0.1s
# settle), but this gives a slow `claude` cold start real headroom before a
# session still mid-launch could be mistaken for a zombie.
MIN_AGE_SECONDS = 60

_SHELL_COMMANDS = frozenset({"zsh", "bash", "sh", "fish", "tcsh", "csh", "dash"})


def _is_bare_shell(command: str) -> bool:
    """True if *command* (a ``pane_current_command`` value) is a login shell."""
    return command.strip().lstrip("-") in _SHELL_COMMANDS


def scan() -> list[dict]:
    """Live scheduler-dispatched worktree sessions stuck at a bare shell.

    Each entry: ``session``, ``branch``, ``command``, ``age_seconds``.
    """
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_created}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []

    now = time.time()
    zombies = []
    for line in result.stdout.strip().splitlines():
        if "\t" not in line:
            continue
        session, created = line.split("\t", 1)
        _, branch, machine = parse_session_name(session)
        if machine or not branch or not branch.startswith(BRANCH_PREFIX):
            continue
        try:
            age = now - float(created)
        except ValueError:
            continue
        if age < MIN_AGE_SECONDS:
            continue

        panes = subprocess.run(
            ["tmux", "list-panes", "-t", f"={session}", "-F", "#{pane_current_command}"],
            capture_output=True, text=True,
        )
        if panes.returncode != 0:
            continue
        commands = [p for p in panes.stdout.strip().splitlines() if p]
        if len(commands) == 1 and _is_bare_shell(commands[0]):
            zombies.append({
                "session": session, "branch": branch,
                "command": commands[0], "age_seconds": int(age),
            })
    return zombies


def _pane_tail(session: str, lines: int = 3) -> str:
    """The last few rendered lines of a zombie's pane, joined with ' / '.

    Captured BEFORE the kill, because the kill is what destroys the only
    record of why the launch never reached its agent (#856: a launch line
    truncated at the tty's canonical-input cap left an unterminated
    `--append-system-prompt "$(<…` sitting at a continuation prompt — visible
    here, invisible everywhere else). Best-effort: "" when tmux can't answer.
    """
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", f"={session}", "-p", "-S", "-40"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return ""
        rendered = [ln.rstrip() for ln in result.stdout.splitlines() if ln.strip()]
        return " / ".join(ln.strip()[:200] for ln in rendered[-lines:])
    except Exception:
        return ""


def _notify(session: str, branch: str, command: str, pane_tail: str = "") -> None:
    """Alert on a reaped zombie session (#739 + #743).

    Always posts a portal toast, so the reap is visible in-band rather than
    only in an email the human has to notice and forward. When the session
    has a recorded parent (``created_by`` in its metadata — set at
    ``agentwire new`` time, see ``_record_session_creator``), the crash is
    ALSO escalated there via the msg inbox (drained by the same watchdog that
    runs this reap) — a worktree worker's crash reaches its orchestrator, not
    just the owner's inbox. Owner email — the reused Resend wiring, mirrors
    ``dispatch._notify_dispatch_timeout`` — is strictly the fallback for a
    session with no recorded parent (the genuine root/scheduler-dispatch
    case #739 originally covered). Each channel is independently
    best-effort; this must never raise into the caller.
    """
    text = (
        f"agentwire: reaped zombie scheduler session `{session}` (branch "
        f"`{branch}`) — launch crashed at a bare shell (`{command}`) before "
        "the agent started."
    )
    if pane_tail:
        text += f" Pane tail: {pane_tail}"

    try:
        from ..core import _post_desktop_notification
        _post_desktop_notification(text, session=session, priority="high")
    except Exception:
        pass

    parent = None
    try:
        from ..core import load_session_metadata
        parent = load_session_metadata(session).get("created_by") or None
    except Exception:
        parent = None

    if parent:
        try:
            from ..inbox import enqueue
            enqueue(parent, text, kind="escalation", sender="agentwire")
        except Exception:
            pass
        return

    try:
        import socket

        from ..channels.email import send_email
        send_email(
            subject=f"[agentwire] reaped zombie scheduler session: {session}",
            body=(
                f"Scheduler dispatch session `{session}` (branch `{branch}`) "
                f"on `{socket.gethostname()}` never reached its agent — the "
                f"pane was stuck at a bare shell (`{command}`), the #739 "
                "failure mode where the worktree launch crashes before "
                "`claude` starts (e.g. a missing worktree directory). The "
                "watchdog killed the session so it can't linger.\n\n"
                + (f"Pane tail before the kill:\n\n    {pane_tail}\n\n" if pane_tail else "")
                + "Check the scheduler events log for the originating "
                "dispatch failure."
            ),
        )
    except Exception:
        pass


def reap() -> dict:
    """Kill every detected zombie session, logging + emailing each one."""
    from agentwire import scheduler as _sched

    killed = []
    for z in scan():
        # Read the pane BEFORE killing it — the kill is what erases the only
        # evidence of why the launch never reached its agent (#856).
        pane_tail = _pane_tail(z["session"])
        _sched._kill_session(z["session"])
        _sched._log_event("zombie_session_reaped", session=z["session"],
                          branch=z["branch"], command=z["command"],
                          age_seconds=z["age_seconds"], pane_tail=pane_tail)
        _notify(z["session"], z["branch"], z["command"], pane_tail)
        killed.append(z["session"])
    return {"killed": killed}


def tick() -> dict:
    """Watchdog stage entry point — same ``{"killed": [...]}`` shape as ``reap()``."""
    return reap()
