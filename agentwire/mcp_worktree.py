"""MCP tools — worktree domain."""

from .mcp_core import (
    get_caller_session,
    mcp,
    run_agentwire_cmd,
)


@mcp.tool()
def worktree_create(
    name: str,
    project_dir: str = "",
    roles: str = "",
    base: str = "",
    prompt: str = "",
) -> str:
    """Create a worktree session (new branch + checkout + tmux session), optionally seeded.

    The spawn half of the worktree lifecycle (paired with worktree_status /
    worktree_list / worktree_remove). Creates a branch off origin/<base>, a
    worktree under ~/worktrees/, and a tmux session running an agent with the
    worktree-session safety etiquette auto-injected. This is how a Briefing Mode
    anchor fans out correspondents.

    Args:
        name: Worktree/branch name (becomes the branch + session suffix).
        project_dir: Path to the git repo (default: server cwd).
        roles: Comma-separated roles STACKED on the worktree-session etiquette
            (e.g. "correspondent"). Never replaces the safety rail.
        base: Base branch to fork from (default: the repo's origin/HEAD).
        prompt: Optional first message — delivered once the agent is booted and
            ready (verified paste). Lets you spawn AND seed the task in one call
            instead of a separate session_send.

    Returns:
        Success message with the session name + worktree path, or an error.
    """
    args = ["worktree", name]
    if project_dir:
        args += ["-p", project_dir]
    if roles:
        args += ["--roles", roles]
    if base:
        args += ["--base", base]
    if prompt:
        args += ["--prompt", prompt]
    # Forward the calling session as the new worktree's creator so prompt
    # routing (resolve_parent) and notify-parent resolve back to the spawner.
    # The CLI's own get_current_session() can't see the caller through the MCP
    # subprocess boundary, so we pass it explicitly (issue #578).
    caller = get_caller_session()
    if caller:
        args += ["--created-by", caller]
    # Seeding waits for agent boot (~up to 60s); give the CLI room to finish.
    data = run_agentwire_cmd(args, timeout=90)
    if not data.get("success"):
        return f"Failed to create worktree: {data.get('error', 'Unknown error')}"
    session = data.get("session", name)
    path = data.get("path", "")
    seeded = " (seeded)" if prompt and data.get("first_message_delivered") else ""
    if data.get("reattached"):
        return f"Reattached to existing worktree session '{session}' at {path}."
    return f"Created worktree session '{session}'{seeded} at {path}."


@mcp.tool()
def worktree_list(project_dir: str = "") -> str:
    """List worktree sessions for a repo, each with read-only git status.

    Use this to see the state of in-flight worktree work before tearing it
    down — which sessions are alive, and whether each worktree is clean and
    pushed. Git status is local-only (no network): dirty/ahead/behind/pushed.

    Args:
        project_dir: Path to the git repo. Defaults to the server's cwd; pass a
            repo path to scope the list to that project.

    Returns:
        Formatted list of worktree sessions, or a message if none are registered.
    """
    args = ["worktree", "--list"]
    if project_dir:
        args += ["--project", project_dir]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to list worktrees: {data.get('error', 'Unknown error')}"
    entries = data.get("entries", [])
    if not entries:
        return "No worktree sessions registered."
    lines = ["Worktree sessions:"]
    for e in entries:
        state = "live" if e.get("alive") else ("orphan" if e.get("exists") else "stale")
        git = e.get("git") or {}
        badge = ""
        if git.get("exists"):
            bits = ["dirty" if git.get("dirty") else "clean"]
            if not git.get("upstream"):
                bits.append("no-upstream")
            else:
                if git.get("ahead"):
                    bits.append(f"ahead {git['ahead']}")
                if git.get("behind"):
                    bits.append(f"behind {git['behind']}")
                if git.get("pushed") and not git.get("ahead"):
                    bits.append("pushed")
            badge = f" [{', '.join(bits)}]"
        lines.append(f"  {e.get('session')} ({state}) branch={e.get('branch')}{badge}")
    return "\n".join(lines)


@mcp.tool()
def worktree_status(name: str, project_dir: str = "") -> str:
    """Read-only git status for one worktree session (no network, no mutation).

    Reports whether the worktree is clean and whether its branch is pushed —
    use it to confirm the agent finished committing/pushing/PR'ing before you
    call worktree_remove. This tool NEVER commits, pushes, or otherwise writes.

    Args:
        name: Worktree session name, branch, or short name.
        project_dir: Path to the git repo (default: server cwd).

    Returns:
        Git status summary, or an error description.
    """
    args = ["worktree", "--status", name]
    if project_dir:
        args += ["--project", project_dir]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to get worktree status: {data.get('error', 'Unknown error')}"
    if not data.get("exists"):
        return f"Worktree path missing for '{name}' ({data.get('worktree_path')})."
    bits = ["dirty" if data.get("dirty") else "clean"]
    if data.get("dirty"):
        bits[0] += f" (+{data.get('staged', 0)}/~{data.get('unstaged', 0)}/?{data.get('untracked', 0)})"
    if not data.get("upstream"):
        bits.append("no upstream (not pushed)")
    else:
        if data.get("ahead"):
            bits.append(f"ahead {data['ahead']}")
        if data.get("behind"):
            bits.append(f"behind {data['behind']}")
        if data.get("pushed") and not data.get("ahead"):
            bits.append("pushed")
    alive = "alive" if data.get("alive") else "no session"
    return f"{data.get('session')} [{alive}] branch={data.get('branch')}: {', '.join(bits)}"


@mcp.tool()
def worktree_remove(name: str, project_dir: str = "") -> str:
    """Tear down a worktree session: kill the session, remove the worktree + branch, unregister.

    This is the teardown step. The agent should have already committed, pushed,
    and opened its PR (confirm with worktree_status first). This kills the tmux
    session, force-removes the git worktree, and drops the registry entry — it
    does NOT push or open a PR for you.

    Args:
        name: Worktree session name, branch, or short name.
        project_dir: Path to the git repo (default: server cwd).

    Returns:
        Success message describing what was removed, or an error description.
    """
    args = ["worktree", "--remove", name]
    if project_dir:
        args += ["--project", project_dir]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to remove worktree: {data.get('error', 'Unknown error')}"
    session = data.get("session", name)
    killed = " (killed live session)" if data.get("killed") else ""
    if data.get("worktree_removed"):
        return f"Removed worktree session '{session}'{killed}; worktree deleted."
    return f"Unregistered '{session}'{killed}; worktree left at {data.get('path')} (not removed)."


@mcp.tool()
def worktree_prune(project_dir: str = "") -> str:
    """Garbage-collect stale worktree registry entries (+ `git worktree prune`).

    Drops registry entries whose worktree dir is gone and runs git's own prune.
    Housekeeping for an anchor that has spun up and torn down many correspondents.

    Args:
        project_dir: Path to the git repo (default: server cwd).

    Returns:
        Which stale entries were pruned, or that there was nothing to prune.
    """
    args = ["worktree", "--prune"]
    if project_dir:
        args += ["--project", project_dir]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to prune worktrees: {data.get('error', 'Unknown error')}"
    pruned = data.get("pruned") or []
    if not pruned:
        return "Nothing to prune."
    return f"Pruned {len(pruned)} stale entr{'y' if len(pruned) == 1 else 'ies'}: {', '.join(pruned)}"
