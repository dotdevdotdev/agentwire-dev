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
    created_by: str = "",
    kind: str = "",
) -> str:
    """Create a worktree session (new branch + checkout + tmux session), optionally seeded.

    The spawn half of the worktree lifecycle (paired with worktree_status /
    worktree_list / worktree_remove). Creates a branch off origin/<base>, a
    worktree at ~/worktrees/<project>/<name>/, and a tmux session running an
    agent with the intrinsic safety etiquette for its role auto-injected.
    This is how a Briefing Mode anchor fans out correspondents.

    By default, the new session is recorded as the caller's child (so
    notify-parent / prompt routing resolve back to you) ONLY when project_dir
    is the same repo you're running in — a worktree spawned into a genuinely
    separate project becomes its own standalone root instead (#715).

    Args:
        name: Worktree/branch name (becomes the branch + session suffix).
        project_dir: Path to the git repo (default: server cwd).
        roles: Comma-separated roles STACKED on the intrinsic worker etiquette
            (e.g. "correspondent"). Never replaces the safety rail (kind=worker;
            for kind="orchestrator" these REPLACE the default persona instead).
        base: Base branch to fork from (default: the repo's origin/HEAD).
        prompt: Optional first message — delivered once the agent is booted and
            ready (verified paste). Lets you spawn AND seed the task in one call
            instead of a separate session_send.
        created_by: Force a specific recorded creator/parent, overriding the
            default same-project-only inheritance above — e.g. to parent a
            worktree in a closely related project. Leave empty for the
            default behavior; note that empty here is NOT the same as the
            CLI's `--created-by ''` (force standalone) — there is currently
            no way to force standalone through this tool. Ignored when
            kind="orchestrator" and this is left empty — that combination
            roots by default (a durable orchestrator shouldn't inherit
            whoever spawned it).
        kind: Session role — "worker" (default: safety-railed, isolation/
            verify/draft-PR/notify, non-overridable) or "orchestrator" (a
            durable, replaceable-persona project window instead of a
            subordinate). Leave empty for the default.

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
    if kind:
        args += ["--kind", kind]
    if created_by:
        args += ["--created-by", created_by]
    else:
        # Forward the calling session as a CANDIDATE parent — the CLI only
        # inherits it when project_dir turns out to be the caller's own repo
        # (#715); a genuinely separate project becomes its own root. The CLI
        # subprocess can't reliably auto-detect the caller across the MCP
        # boundary, so we resolve and pass it explicitly (issue #578).
        caller = get_caller_session()
        if caller:
            args += ["--caller-session", caller]
    # Seeding waits for agent boot (up to 60s) and, on a failed seed, runs the
    # clear-box + inbox-fallback recovery (#695) — give the CLI room to finish
    # the loud-failure path instead of masking it with a subprocess timeout.
    data = run_agentwire_cmd(args, timeout=180)
    if not data.get("success"):
        return f"Failed to create worktree: {data.get('error', 'Unknown error')}"
    session = data.get("session", name)
    path = data.get("path", "")
    if data.get("reattached"):
        note = (
            " NOTE: the session was already live, so the seed prompt was NOT "
            "pasted — send it with msg_send if still needed."
            if prompt
            else ""
        )
        return f"Reattached to existing worktree session '{session}' at {path}.{note}"
    # Seed failure must be LOUD (#695): a silently-missing "(seeded)" suffix is
    # invisible to an orchestrator skimming results, and the session then sits
    # idle with the task never delivered.
    if prompt and not data.get("first_message_delivered"):
        if data.get("first_message_fallback") == "inbox":
            return (
                f"Created worktree session '{session}' at {path}. "
                "WARNING: seed prompt NOT delivered — the input box was not ready. "
                "The prompt was queued to the session's msg inbox and the watchdog "
                "will deliver it once the box is ready; verify with msg_inbox / "
                "session_output before assuming the task started."
            )
        return (
            f"Created worktree session '{session}' at {path}. "
            "WARNING: seed prompt NOT delivered and could NOT be queued — the "
            f"session is idle with no task. Deliver it manually (session_send to "
            f"'{session}')."
        )
    seeded = " (seeded)" if prompt else ""
    return f"Created worktree session '{session}'{seeded} at {path}."


@mcp.tool()
def worktree_list(project_dir: str = "", check_dangling: bool = False) -> str:
    """List worktree sessions for a repo, each with read-only git status.

    Use this to see the state of in-flight worktree work before tearing it
    down — which sessions are alive, and whether each worktree is clean and
    pushed. Git status is local-only (no network): dirty/ahead/behind/pushed.

    Args:
        project_dir: Path to the git repo. Defaults to the server's cwd; pass a
            repo path to scope the list to that project.
        check_dangling: Also flag LIVE worker-kind sessions with an OPEN PR
            and no live parent — a PR nobody is positioned to review/merge
            (uses `gh`, one network call per live session; off by default).
            Orchestrator-kind sessions are excluded — a self-rooted
            orchestrator with an open PR is its normal healthy lifecycle.
            Distinct from the "orphan" state below (a dead session whose
            worktree dir is left on disk).

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
    if check_dangling:
        dargs = ["worktree", "--dangling"]
        if project_dir:
            dargs += ["--project", project_dir]
        ddata = run_agentwire_cmd(dargs)
        dangling = ddata.get("dangling") or [] if ddata.get("success") else []
        if dangling:
            lines.append("")
            lines.append(f"DANGLING ({len(dangling)}) — open PR, no live parent to review/merge:")
            for d in dangling:
                lines.append(f"  {d.get('session')} branch={d.get('branch')} {d.get('pr_url', '')} ({d.get('reason')})")
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
def worktree_remove(
    name: str,
    project_dir: str = "",
    keep_branch: bool = False,
    force_delete_branch: bool = False,
) -> str:
    """Tear down a worktree session: kill the session, remove the worktree + branch, unregister.

    This is the teardown step. The agent should have already committed, pushed,
    and opened its PR (confirm with worktree_status first). This kills the tmux
    session, force-removes the git worktree (verifying the dir is actually gone —
    never a silent partial teardown), drops the registry entry, and best-effort
    deletes the branch (local ref + remote) once its PR is confirmed merged. It
    never touches main and never requires switching the primary checkout —
    branch deletion works entirely from the (now-vacated) branch name.

    If the torn-down session tracked any claude-in-chrome verification tabs
    (`chrome_tab_track`) it never closed itself, they're reported here — YOU
    must close them with `tabs_close_mcp`; agentwire has no way to do it for you.

    Args:
        name: Worktree session name, branch, or short name.
        project_dir: Path to the git repo (default: server cwd).
        keep_branch: Skip branch cleanup entirely (leave the branch as-is).
        force_delete_branch: Delete the branch even if not confirmed merged —
            use only when you're sure the work is safe to discard.

    Returns:
        Success message describing what was removed, or a loud failure
        description if the worktree directory could not actually be cleared.
    """
    args = ["worktree", "--remove", name]
    if project_dir:
        args += ["--project", project_dir]
    if keep_branch:
        args += ["--keep-branch"]
    if force_delete_branch:
        args += ["--force-delete-branch"]
    data = run_agentwire_cmd(args)
    session = data.get("session", name)
    killed = " (killed live session)" if data.get("killed") else ""
    if not data.get("success"):
        return f"FAILED to remove worktree session '{session}'{killed}: {data.get('error', 'Unknown error')}"
    branch_bit = ""
    if data.get("branch"):
        branch_bit = (f" Branch '{data['branch']}' deleted." if data.get("branch_deleted")
                      else f" Branch '{data['branch']}' kept ({data.get('branch_note', 'not deleted')}).")
    tabs_bit = _orphaned_tabs_warning(data.get("orphaned_tabs"))
    return f"Removed worktree session '{session}'{killed}; worktree deleted.{branch_bit}{tabs_bit}"


def _orphaned_tabs_warning(orphaned: list | None) -> str:
    """Format a WARNING suffix for claude-in-chrome tabs a torn-down session
    never closed itself — agentwire can't close them (that MCP tool only runs
    inside the calling agent's own client), so the calling agent must (#717).
    Entries carrying a "session" key (the --gc-merged multi-session sweep) are
    attributed per-tab; single-session worktree_remove entries just list ids.
    """
    if not orphaned:
        return ""
    ids = ", ".join(
        f"{t['session']}:{t.get('tab_id', '?')}" if t.get("session") else t.get("tab_id", "?")
        for t in orphaned
    )
    return (f" WARNING: {len(orphaned)} claude-in-chrome tab(s) opened by torn-down session(s) were "
            f"never closed — call tabs_close_mcp for: {ids}.")


@mcp.tool()
def worktree_prune(project_dir: str = "", gc_merged: bool = False) -> str:
    """Garbage-collect stale worktree registry entries (+ `git worktree prune`).

    Drops registry entries whose worktree dir is gone and runs git's own prune.
    Housekeeping for an anchor that has spun up and torn down many correspondents.

    Args:
        project_dir: Path to the git repo (default: server cwd).
        gc_merged: Also fully tear down (session + worktree + branch, via the
            same atomic path as worktree_remove) any STILL-PRESENT registered
            worktree whose branch is confirmed merged. Off by default — this
            is otherwise a safe, read-mostly cleanup and shouldn't kill a
            live, in-flight session just because its branch looks merged.

    Returns:
        Which stale entries were pruned / merged worktrees GC'd, or that
        there was nothing to prune.
    """
    args = ["worktree", "--prune"]
    if project_dir:
        args += ["--project", project_dir]
    if gc_merged:
        args += ["--gc-merged"]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to prune worktrees: {data.get('error', 'Unknown error')}"
    pruned = data.get("pruned") or []
    gc_done = data.get("gc_merged") or []
    if not pruned and not gc_done:
        return "Nothing to prune."
    bits = []
    if pruned:
        bits.append(f"Pruned {len(pruned)} stale entr{'y' if len(pruned) == 1 else 'ies'}: {', '.join(pruned)}")
    if gc_done:
        bits.append(f"GC'd {len(gc_done)} merged worktree{'s' if len(gc_done) != 1 else ''}: {', '.join(gc_done)}")
    orphaned = data.get("orphaned_tabs") or []
    tabs_bit = _orphaned_tabs_warning(orphaned).strip()
    return "; ".join(bits) + "." + ((" " + tabs_bit) if tabs_bit else "")
