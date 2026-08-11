"""MCP tools — session domain."""

from .core import run_agentwire_cmd
from .mcp_core import (
    _delivery_result,
    _mcp_result,
    format_sessions,
    get_caller_session,
    mcp,
)


@mcp.tool()
def sessions_list() -> str:
    """List all active AgentWire sessions.

    Returns information about all tmux sessions including name, machine,
    window count, working directory, and session type.

    Returns:
        Formatted list of sessions or error message.
    """
    data = run_agentwire_cmd(["list", "--sessions"])
    if not data.get("success"):
        return f"Failed to list sessions: {data.get('error', 'Unknown error')}"
    return format_sessions(data)


def format_session_contexts(data: dict, session_filter: str | None = None) -> str:
    """Format per-session context headroom for LLM consumption.

    The bar % is context REMAINING (headroom before Claude Code auto-compacts),
    not usage — so LOW = bloated. Daemons (scheduler/portal/tts/…) have no bar
    and are reported as non-interactive.
    """
    sessions = data.get("sessions", [])
    if session_filter:
        sessions = [s for s in sessions if s.get("name", "").split("@")[0] == session_filter]
        if not sessions:
            return f"No session named '{session_filter}'."
    if not sessions:
        return "No active sessions."

    def sort_key(s):
        ctx = s.get("context") or {}
        pct = ctx.get("remaining_pct")
        return (not ctx.get("is_agent"), pct if pct is not None else 999, s.get("name", ""))

    flagged = []
    lines = ["Session context (remaining % = headroom; LOW = bloated):"]
    for s in sorted(sessions, key=sort_key):
        name = s.get("name", "unknown")
        ctx = s.get("context")
        if ctx is None:
            lines.append(f"  - {name}: (remote — context n/a)")
            continue
        pct = ctx.get("remaining_pct")
        if pct is None:
            kind = "daemon/non-agent" if not ctx.get("is_agent") else "agent, no bar visible"
            lines.append(f"  - {name}: — ({kind})")
            continue
        model = f" {ctx['model']}" if ctx.get("model") else ""
        flag = "  ⚠ LOW — running out of context" if ctx.get("flagged") else ""
        lines.append(f"  - {name}:{model} {pct}% context remaining{flag}")
        if ctx.get("flagged"):
            flagged.append(name)

    if flagged:
        lines.append("")
        lines.append(f"⚠ {len(flagged)} session(s) low on context: {', '.join(flagged)}")
        lines.append("(Observe-only — Phase 0. No auto-/clear or /compact is performed.)")
    return "\n".join(lines)


@mcp.tool()
def sessions_context(session: str | None = None) -> str:
    """Report Claude Code context headroom for sessions (observability only).

    Each interactive session's footer shows a context bar whose percentage is
    the context REMAINING before Claude Code auto-compacts — so a LOW number
    means the session is bloated. Daemon sessions (scheduler/portal/tts/stt)
    run plain processes with no bar and are reported as non-interactive.

    This is Phase 0 of context-bloat management: it makes bloat visible and
    queryable. It NEVER auto-/clear or /compacts a session.

    Args:
        session: Optional single session name to report. Omit for all sessions
            (sorted most-bloated first).

    Returns:
        Formatted per-session context state, with low sessions flagged.
    """
    data = run_agentwire_cmd(["list", "--context"])
    if not data.get("success"):
        return f"Failed to read session context: {data.get('error', 'Unknown error')}"
    return format_session_contexts(data, session_filter=session)


@mcp.tool()
def session_create(
    name: str,
    project_dir: str | None = None,
    roles: str | None = None,
    posture: str | None = None,
    base: str | None = None,
    pull_first: bool = True,
    created_by: str = "",
    standalone: bool = False,
    kind: str = "",
) -> str:
    """Create a new AgentWire session.

    Worktree mode: use 'project/branch' as the name (e.g. 'fragmentz/nav-dropdown-185')
    to fork an isolated git worktree off `base` (default 'main') at
    `<project_dir>-worktrees/<branch>/`. This is the safe way to spin up parallel
    agents on the same repo — each gets its own working tree and branch.

    Flat names (no slash) attach a session directly to `project_dir` as-is. If
    that path is already the working directory of another active session, the
    call is refused — two agents on the same dirty tree is unsafe.

    The new session is recorded as your child (so notify-parent / prompt
    routing resolve back to you) ONLY when project_dir is the same repo
    you're running in — a session spawned in a genuinely separate project
    becomes its own standalone root instead (#715).

    Args:
        name: Session name. Use 'project/branch' for an isolated worktree;
            a flat name attaches to project_dir directly.
        project_dir: Project directory path. For worktree mode, this is the main
            repo (the worktree is created alongside it). Optional.
        roles: Comma-separated list of roles to apply. Optional.
        posture: Permission mode: bypass | prompted | auto (or bare). Optional.
        base: Base branch to fork the worktree from (worktree mode only,
            default 'main'). Ignored for flat names.
        pull_first: Fetch origin/<base> before branching (worktree mode only,
            default True). Set False to branch off the local <base> as-is.
        created_by: Force a specific recorded creator/parent, overriding the
            default same-project-only inheritance above — e.g. to parent a
            session in a closely related project. Leave empty for the
            default behavior. Wins over `standalone` when both are given.
            Ignored when kind="orchestrator" is passed explicitly and this
            is left empty — that combination roots by default instead (a
            durable orchestrator shouldn't inherit whoever spawned it),
            regardless of whether name has a branch.
        standalone: Force a standalone root (no parent) even in your OWN
            project, passing the CLI's `--created-by ''`. Use this when you're
            spawning a session in a separate project you're only advising —
            so its prompts route to the human, not back to you. Cross-project
            spawns already auto-root (#715), so this is mainly for a
            same-project session you want detached, or to force standalone
            explicitly. Ignored when `created_by` is set (that wins).
        kind: Session role — "worker", "orchestrator", or "reviewer". A flat
            name (no branch) already defaults to orchestrator; a
            project/branch name defaults to worker (safety-railed:
            isolation/verify/draft-PR/notify). Pass "orchestrator" to
            override that default for a project/branch name — e.g. a durable
            branch-pinned project window. Pass "reviewer" for a PR-review
            station that adversarially reviews a sibling session's PR and
            never opens/merges one of its own (safety-railed the other way —
            non-overridable, parented rooting like worker, not rooted like
            orchestrator). Leave empty for the derived default.

    Returns:
        Success message or error description.
    """
    args = ["new", "-s", name]

    if project_dir:
        args.extend(["-p", project_dir])
    if roles:
        args.extend(["--roles", roles])
    if posture:
        args.extend(["--posture", posture])
    if base:
        args.extend(["--base", base])
    if not pull_first:
        args.append("--no-pull-first")
    if kind:
        args.extend(["--kind", kind])
    if created_by:
        args.extend(["--created-by", created_by])
    elif standalone:
        # Force a standalone root even in the caller's own project (#712):
        # the CLI reads `--created-by ''` (an explicit empty string, which
        # argparse distinguishes from the flag being absent) as "opt out of
        # inheritance". Wins over the default caller-forwarding below;
        # `created_by` above wins over this.
        args.extend(["--created-by", ""])
    else:
        # Forward the calling session as a CANDIDATE parent — the CLI only
        # inherits it when project_dir turns out to be the caller's own repo
        # (#715); a genuinely separate project becomes its own standalone
        # root. The CLI subprocess can't reliably auto-detect the caller
        # across the MCP boundary, so we resolve and pass it explicitly
        # (issue #578).
        caller = get_caller_session()
        if caller:
            args.extend(["--caller-session", caller])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Session '{name}' created successfully."
    return f"Failed to create session: {data.get('error', 'Unknown error')}"


@mcp.tool()
def session_send(session: str, message: str) -> str:
    """Send a prompt/message to a session.

    Automatically includes the sender's session name so the receiving
    agent knows who sent the message and can reply via session_send.

    Args:
        session: Session name (can include @machine suffix for remote)
        message: The message to send (Enter key is appended automatically)

    Returns:
        Success message or error description.
    """
    caller = get_caller_session()
    if caller and caller != session:
        message = (
            f"[MESSAGE FROM SESSION \"{caller}\" — to reply, call "
            f"session_send(session=\"{caller}\", message=\"<your reply>\")]\n"
            f"{message}"
        )
    args = ["send", "-s", session, "--verify", message]
    if caller:
        # Attributes a msg-inbox delivery fallback to the real caller instead
        # of the generic "agentwire" (#835 review) — the CLI subprocess can't
        # auto-detect the caller across the MCP boundary itself.
        args.extend(["--caller-session", caller])
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to send message: {data.get('error', 'Unknown error')}"
    return _delivery_result(data, f"to session '{session}'")


@mcp.tool()
def session_output(session: str, lines: int = 50) -> str:
    """Capture output from a session.

    Args:
        session: Session name (can include @machine suffix for remote)
        lines: Number of lines to capture (default: 50)

    Returns:
        The captured output from the session.
    """
    args = ["output", "-s", session, "-n", str(lines)]
    data = run_agentwire_cmd(args)
    if data.get("success"):
        return data.get("output", "")
    return f"Failed to capture output: {data.get('error', 'Unknown error')}"


@mcp.tool()
def diff(session: str, base: str = "") -> str:
    """Summarize a session's git diff (what the agent changed).

    Thin wrapper over `agentwire diff`. Use this to review a worktree
    session's work before approving it. The portal's mobile Review window
    renders the same data with tap-to-approve controls.

    Args:
        session: Session name to inspect.
        base: Diff base ref. Empty = HEAD if there are uncommitted changes,
            else origin/main (a worktree session's committed branch work).

    Returns:
        A per-file summary of additions/deletions, or "No changes".
    """
    args = ["diff", "-s", session]
    if base:
        args += ["--base", base]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to get diff: {data.get('error', 'Unknown error')}"
    files = data.get("files", [])
    if not files:
        return f"No changes vs {data.get('base')}."
    lines = [
        f"Diff vs {data.get('base')} "
        f"(+{data.get('additions')} -{data.get('deletions')}):"
    ]
    for f in files:
        lines.append(
            f"  {f['status']}: {f['path']} (+{f['additions']} -{f['deletions']})"
        )
    if data.get("truncated"):
        lines.append("  … diff truncated (review at a desk)")
    return "\n".join(lines)


@mcp.tool()
def session_info(session: str) -> str:
    """Get detailed information about a session.

    Args:
        session: Session name (can include @machine suffix for remote)

    Returns:
        Session metadata including working directory, pane count, etc.
    """
    args = ["info", "-s", session]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to get session info: {data.get('error', 'Unknown error')}"

    # Format the info nicely
    lines = [f"Session: {session}"]
    if cwd := data.get("cwd"):
        lines.append(f"  Working directory: {cwd}")
    if panes := data.get("panes"):
        lines.append(f"  Panes: {len(panes)}")
    if posture := data.get("posture"):
        lines.append(f"  Posture: {posture}")
    if roles := data.get("roles"):
        lines.append(f"  Roles: {', '.join(roles)}")

    return "\n".join(lines)


@mcp.tool()
def session_kill(session: str) -> str:
    """Terminate a session.

    Args:
        session: Session name (can include @machine suffix for remote)

    Returns:
        Success message or error description.
    """
    return _mcp_result(run_agentwire_cmd(["kill", "-s", session]),
                       f"Session '{session}' terminated.", "kill session")


@mcp.tool()
def session_send_keys(session: str, keys: list[str]) -> str:
    """Send raw keys to a session without automatic Enter.

    Useful for sending control sequences like Ctrl-C, Escape, Enter,
    arrow keys, etc. Each key group is sent with a brief pause between.

    Args:
        session: Session name (can include @machine suffix for remote)
        keys: List of key groups to send (e.g., ["Ctrl-C", "Enter"])

    Returns:
        Success message or error description.
    """
    args = ["send-keys", "-s", session] + keys
    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Sent {len(keys)} key group(s) to '{session}'."
    return f"Failed to send keys: {data.get('error', 'Unknown error')}"


@mcp.tool()
def session_recreate(session: str) -> str:
    """Destroy and recreate a session with a fresh worktree.

    Useful when a session is in a bad state and needs a clean start.

    Args:
        session: Session name to recreate

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["recreate", "-s", session], timeout=180)
    if data.get("success"):
        return f"Session '{session}' recreated with fresh worktree."
    return f"Failed to recreate session: {data.get('error', 'Unknown error')}"


@mcp.tool()
def session_fork(session: str, target: str, commit: str = "") -> str:
    """Fork a session into a new worktree.

    Creates a new session based on an existing one, with its own
    git worktree for isolated work.

    Args:
        session: Source session name (project or project/branch)
        target: Target session name (must include branch: project/new-branch)
        commit: Optional commit/ref to fork from instead of HEAD (e.g. abc123, main~5)

    Returns:
        Success message or error description.
    """
    cmd = ["fork", "-s", session, "-t", target]
    if commit:
        cmd += ["--commit", commit]
    data = run_agentwire_cmd(cmd, timeout=120)
    if data.get("success"):
        forked = data.get("session", target)
        return f"Session '{session}' forked to '{forked}'."
    return f"Failed to fork session: {data.get('error', 'Unknown error')}"


@mcp.tool()
def wait_children(timeout: int = 300) -> str:
    """Block until the child sessions YOU spawned report back, then reap them.

    Use this after fanning out work with session_create / worktree_create,
    instead of polling or "waiting" idly. Waiting idly is what gets a fan-out
    parent reaped mid-flight: the idle handler reads idle as done, prompts for
    a roll-up you can't write yet, and then /exit-s the session while its
    children are still working (#852). This call blocks INSIDE a tool call,
    which is not idle, so the handler never fires.

    Each pass collects any report waiting in your inbox, tears down the child
    it collected, and marks a child whose session has already exited. Children
    still outstanding when the cohort deadline passes are torn down too and
    returned as failures — name them in your roll-up rather than pretending
    the run was clean.

    Bounded and re-callable: if it returns with children still pending, call
    it again to keep waiting.

    Args:
        timeout: Seconds to block in THIS call (default: 300)

    Returns:
        The children's reports, plus any that failed or are still pending.
    """
    args = ["wait", "--children", "--timeout", str(timeout)]
    caller = get_caller_session()
    if caller:
        args.extend(["-s", caller])
    data = run_agentwire_cmd(args, timeout=timeout + 30)
    if not data.get("success"):
        return f"Failed to wait on children: {data.get('error', 'Unknown error')}"
    if not data.get("cohort"):
        return "No fan-out cohort registered for this session — nothing to wait on."

    lines = []
    for entry in data.get("reports") or []:
        lines.append(f"── {entry['session']} ──\n{entry.get('report') or '(no report text)'}")
    for entry in data.get("failed") or []:
        why = "never reported" if entry.get("state") == "timeout" else "session gone"
        lines.append(f"── {entry['session']} ── FAILED: {why}")
    left = data.get("left_alive") or []
    if left:
        lines.append(
            "Left running (worktree children — their branch/PR teardown follows merge "
            f"verification, #756): {', '.join(left)}")
    pending = data.get("pending") or []
    if pending:
        lines.append(
            f"Still pending: {', '.join(pending)} — call wait_children again to keep waiting.")
    elif not lines:
        lines.append("Cohort resolved with no reports.")
    return "\n\n".join(lines)
