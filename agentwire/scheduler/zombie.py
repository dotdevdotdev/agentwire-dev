"""Reap bare-shell scheduler sessions (#739).

``_dispatch_worktree_task`` names every worktree branch
``scheduler-<task>-<ts>``. If the session's launch crashes before the agent
starts (e.g. the worktree directory went missing between ``agentwire new``
reporting success and the pane's ``cd``), the tmux session drops to a bare
shell — which the idle-reaper correctly never touches, since it only reaps a
*running* agent that goes idle. Nothing else would ever clean that up, so it
lingers indefinitely. This module finds and kills those sessions.

Detected by branch naming rather than ``worktree_registry`` — scheduler
dispatch goes through ``agentwire new``, which (unlike ``agentwire
worktree``) never registers there.
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


def _notify(session: str, branch: str, command: str) -> None:
    """Best-effort owner email — reused Resend wiring, mirrors
    ``dispatch._notify_dispatch_timeout`` (never raises into the caller)."""
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
                "Check the scheduler events log for the originating "
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
        _sched._kill_session(z["session"])
        _sched._log_event("zombie_session_reaped", session=z["session"],
                          branch=z["branch"], command=z["command"],
                          age_seconds=z["age_seconds"])
        _notify(z["session"], z["branch"], z["command"])
        killed.append(z["session"])
    return {"killed": killed}


def tick() -> dict:
    """Watchdog stage entry point — same ``{"killed": [...]}`` shape as ``reap()``."""
    return reap()
