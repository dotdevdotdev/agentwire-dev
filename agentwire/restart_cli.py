"""``agentwire restart`` — relaunch a session IN PLACE, same conversation (#871).

The verb that was missing. Restarting a session onto new code (a `rebuild`, a
reboot, a wedged agent) used to mean picking the least-wrong neighbour:

- ``recreate`` force-removes the worktree and ``rm -rf``s the directory, then
  cuts a fresh timestamped branch. It destroys uncommitted work by design.
- ``history resume`` always forks into a NEW tmux session, leaving the old one
  behind and the branch/PR mapping pointing at a session nobody is in.
- ``kill`` + ``new`` loses the conversation entirely, and — before #871 — the
  role with it, since the role prompt lived in a temp file macOS GC'd.

What this does instead: ``/exit`` the session, REGENERATE its launch flags from
the recorded ``roles``/``posture``/``model``, and relaunch at the same cwd with
``--resume`` so the same conversation continues in the same tmux session.

**Regenerate, never re-evaluate.** The launch line is rebuilt through
``build_agent_command``. The tmux session env holds the previous one in
``AGENTWIRE_LAUNCH_CMD``, and re-``eval``ing that is the single most tempting
wrong move here: it carries a SINGLE-USE ``--session-id``, and Claude treats a
reused id as fatal ("Session ID <id> is already in use.") — it refuses to
start, dropping the pane to the bare shell that ``_guarded_launch_command``
exists to prevent. The recorded id is only ever passed to ``--resume``.

**A recorded id does not guarantee a resumable conversation.** ``history.
locate_conversation`` probes for the actual ``.jsonl`` before resuming, and
this verb degrades deliberately when it isn't there — fresh conversation, role
intact, and it SAYS SO. See :func:`resolve_resume_target`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import pane_manager
from .core import (
    _graceful_kill,
    _launch_tmux_session,
    _notify_portal_sessions_changed,
    _output_result,
    _set_session_name_env,
    build_agent_command,
    load_session_metadata,
    record_session_launch,
    tmux_session_exists,
)
from .history import ConversationLocation, locate_conversation
from .roles import load_roles
from .worktree import tmux_safe_name

#: How long to wait for the relaunched agent to become interactive before
#: reporting the restart as unverified. A resume replays the transcript, so
#: it boots slower than a fresh session; 60s matches ``cmd_new``'s wait.
READY_TIMEOUT = 60.0


def resolve_resume_target(
    conversation_ids: list[str], cwd: str
) -> tuple[str | None, ConversationLocation | None]:
    """Newest resumable conversation in the chain, plus what we found.

    Walks the chain NEWEST FIRST and returns the first id whose history
    actually exists under *cwd*'s key. Walking rather than taking the last
    entry is what handles the launched-but-never-prompted session: the
    ``.jsonl`` is created lazily on the first turn, so a session that was
    relaunched and then never spoken to has no file for its newest id, while
    the id it forked FROM still holds the whole conversation. Taking only the
    tail would throw that away and start blank.

    When nothing resolves, returns ``(None, <the location worth reporting>)``:
    the newest ORPHANED entry if the chain holds one, else the newest entry.
    Orphaned outranks gone because it is the recoverable state — and a chain
    whose newest id was never prompted (``gone``) can easily sit on top of an
    orphaned conversation that migration would bring back, so reporting the
    tail alone would hide the one finding anyone can act on.
    """
    newest: ConversationLocation | None = None
    orphaned: ConversationLocation | None = None
    for conversation_id in reversed(conversation_ids):
        loc = locate_conversation(conversation_id, cwd)
        if newest is None:
            newest = loc
        if loc.resumable:
            return conversation_id, loc
        if orphaned is None and loc.status == "orphaned":
            orphaned = loc
    return None, orphaned or newest


def _history_note(loc: ConversationLocation | None, cwd: str) -> str | None:
    """The one-line explanation of why a restart did NOT resume, if it didn't."""
    if loc is None:
        return ("No conversation was ever recorded for this session — "
                "starting fresh with the recorded role intact.")
    if loc.status == "orphaned":
        return (
            f"Conversation {loc.conversation_id[:8]} exists but is ORPHANED: its "
            f"history is under {loc.elsewhere[0].parent.name}, not the key for "
            f"{cwd}. Claude keys history by cwd, so a moved directory strands it. "
            "Starting fresh with the recorded role intact."
        )
    return (
        f"No history found for conversation {loc.conversation_id[:8]} — it was "
        "either never prompted (the transcript is written on the first turn) or "
        "Claude has evicted it. Starting fresh with the recorded role intact."
    )


def cmd_restart(args) -> int:
    """Relaunch a recorded session in place, resuming its conversation."""
    session_arg = getattr(args, "session", None)
    json_mode = getattr(args, "json", False)

    if not session_arg:
        return _output_result(False, json_mode, "Usage: agentwire restart -s <session>")

    if "@" in session_arg:
        return _output_result(
            False, json_mode,
            "restart is local-only: a resume has to probe ~/.claude/projects on the "
            "machine that holds the history. Run it on that machine, or use "
            "`agentwire recreate -s <session>@<machine>` (which discards the "
            "conversation and the worktree).",
        )

    # Same mapping every creation path uses (#868/#878) — the record is keyed
    # by the name tmux actually made, so a raw `proj.x` would look up nothing.
    session_name = tmux_safe_name(session_arg)
    metadata = load_session_metadata(session_name)

    cwd = metadata.get("cwd_at_launch")
    posture = metadata.get("posture")
    if not cwd or not posture:
        return _output_result(
            False, json_mode,
            f"No launch record for '{session_name}' — nothing to regenerate its "
            "flags from. Either it was already killed (`agentwire kill` drops the "
            "record on purpose, so a later session reusing the name can't inherit "
            "a stale parent — a killed session is finished, not restartable), or "
            "it launched before the record existed (#871). Start it with "
            "`agentwire new` / `agentwire worktree`; restart works from then on.",
        )

    if not Path(cwd).exists():
        return _output_result(
            False, json_mode,
            f"Recorded working directory is gone: {cwd}\n"
            "Restarting there would launch the agent from the wrong cwd (and its "
            "conversation history is keyed to that path anyway). If the worktree "
            "moved, see `agentwire worktree --list`; if it was torn down, this "
            "session is finished — use `agentwire new`.",
        )

    # A session cannot restart itself: `/exit` + kill-session would take down
    # the tmux session this very process is running in, so nothing would be
    # left to relaunch. Refuse rather than half-run.
    current = pane_manager.get_current_session()
    if current and current == session_name:
        return _output_result(
            False, json_mode,
            f"'{session_name}' is the session you're running in — restarting it "
            "would kill this process mid-command. Run it from another session, or "
            "ask your parent/orchestrator to restart you.",
        )

    role_names = list(metadata.get("roles") or [])
    roles, missing_roles = load_roles(role_names, Path(cwd)) if role_names else ([], [])
    if missing_roles:
        # Proceed rather than block: refusing would strand the session with no
        # way back, and a partial role beats no session. Loud, though — a
        # missing worker rail is a safety-relevant change to what relaunches.
        print(
            f"Warning: recorded role(s) no longer resolve: {', '.join(missing_roles)}. "
            "Restarting WITHOUT them — the relaunched agent's etiquette differs "
            "from what it was launched with.",
            file=sys.stderr,
        )

    resume_id, location = resolve_resume_target(
        list(metadata.get("conversation_ids") or []), cwd
    )

    agent = build_agent_command(
        posture,
        roles or None,
        model=metadata.get("model"),
        resume_session_id=resume_id,
    )
    _set_session_name_env(agent, session_name, created_by=metadata.get("created_by"))

    was_live = tmux_session_exists(session_name)
    if was_live:
        _graceful_kill(session_name)

    _launch_tmux_session(session_name, cwd, agent.env, agent.command)

    # Append this launch to the record. created_by/created_via/role are
    # deliberately not passed: a restart is not a new creation, and
    # record_session_launch preserves what it isn't told to change.
    record_session_launch(session_name, agent, cwd)
    _notify_portal_sessions_changed()

    # Only a posture with an agent has anything to become ready; `bare` is the
    # no-agent sentinel and its pane is just a shell.
    ready: bool | None = None
    if agent.command and not getattr(args, "no_wait", False):
        from .session_ready import wait_for_session_ready

        if not json_mode:
            print("Waiting for the agent to come back up...")
        ready = wait_for_session_ready(session_name, timeout=READY_TIMEOUT)

    # `bare` launches no agent at all, so there is no conversation to have
    # resumed and nothing to explain about history — saying "starting fresh
    # with the role intact" about a plain shell is just noise.
    note = None if (resume_id or not agent.command) else _history_note(location, cwd)

    if json_mode:
        from .core import _output_json

        _output_json({
            "success": True,
            "session": session_name,
            "path": cwd,
            "resumed_from": resume_id,
            "conversation_id": agent.conversation_id,
            "history_state": location.status if location else "none_recorded",
            "posture": posture,
            "roles": role_names,
            "missing_roles": missing_roles,
            "model": metadata.get("model"),
            "was_live": was_live,
            "agent_ready": ready,
            "note": note,
        })
    else:
        verb = "Restarted" if was_live else "Relaunched (session was not running)"
        print(f"{verb} '{session_name}' in {cwd}")
        if resume_id:
            print(f"  Resumed conversation {resume_id[:8]} → {agent.conversation_id[:8]} "
                  "(--fork-session mints a new id per resume)")
        elif note:
            print(f"  [!!] {note}")
        print(f"  Posture: {posture}   Roles: {', '.join(role_names) or 'none'}"
              + (f"   Model: {metadata['model']}" if metadata.get("model") else ""))
        if ready is False:
            print("  [!!] The agent did not become interactive within "
                  f"{READY_TIMEOUT:g}s — check it with `agentwire diff -s "
                  f"{session_name}` or attach: tmux attach -t {session_name}")
        print(f"  Attach with: tmux attach -t {session_name}")

    # An unverified relaunch is not a success a script should trust.
    return 1 if ready is False else 0


def register_restart_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "restart",
        help="Relaunch a session in place, resuming the same conversation",
        description=(
            "Exit the session, regenerate its launch flags from the recorded "
            "roles/posture/model, and relaunch at the same cwd with --resume. "
            "Unlike `recreate` nothing on disk is touched, and unlike `history "
            "resume` no new tmux session is created. When the conversation's "
            "history is orphaned or gone, restarts fresh with the role intact "
            "and says so."
        ),
    )
    parser.add_argument("--session", "-s", help="Session to restart")
    parser.add_argument(
        "--no-wait", action="store_true",
        help="Don't wait for the relaunched agent to become interactive",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.set_defaults(func=cmd_restart)
