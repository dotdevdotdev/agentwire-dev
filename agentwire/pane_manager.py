"""
Pane management for tmux-based worker agents.

Workers are spawned as panes within the orchestrator's session,
enabling a visual dashboard where all agents are visible simultaneously.

Key concepts:
- Pane 0 = orchestrator (main Claude Code session)
- Panes 1+ = workers (spawned agents working in parallel)
"""

import os
import time
from dataclasses import dataclass

from .utils.subprocess import run_command


@dataclass
class PaneInfo:
    """Information about a tmux pane."""
    index: int
    pane_id: str  # e.g., %37
    pid: int
    command: str
    active: bool = False


def get_current_session() -> str | None:
    """Get the session name from the current tmux environment.

    Returns:
        Session name, or None if not running inside tmux.
    """
    tmux_pane = os.environ.get("TMUX_PANE")
    if not tmux_pane:
        return None

    result = run_command(
        ["tmux", "display", "-t", tmux_pane, "-p", "#{session_name}"],
        timeout=5,
    )
    return result.stdout.strip() if result.success else None


def get_current_pane_index() -> int | None:
    """Get the pane index from the current tmux environment.

    Returns:
        Pane index (0-based), or None if not running inside tmux.
    """
    tmux_pane = os.environ.get("TMUX_PANE")
    if not tmux_pane:
        return None

    result = run_command(
        ["tmux", "display", "-t", tmux_pane, "-p", "#{pane_index}"],
        timeout=5,
    )
    if result.success:
        return int(result.stdout.strip())
    return None


def _get_window_dimensions(session: str) -> tuple[int, int]:
    """Get window width and height for smart split direction.

    Args:
        session: Tmux session name.

    Returns:
        Tuple of (width, height) in characters.
    """
    result = run_command(
        ["tmux", "display", "-t", session, "-p", "#{window_width}:#{window_height}"],
        timeout=5,
    )
    if result.success:
        parts = result.stdout.strip().split(":")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    return 120, 40  # default to landscape


def spawn_worker_pane(
    session: str | None = None,
    cwd: str | None = None,
    cmd: str | None = None
) -> int:
    """Spawn a new pane in the session and return its index.

    Args:
        session: Target session (default: auto-detect from $TMUX_PANE)
        cwd: Working directory for the new pane
        cmd: Command to run in the pane (sent after creation)

    Returns:
        The pane index of the newly created pane.

    Raises:
        RuntimeError: If not in tmux and no session specified, or if spawn fails.
    """
    if session is None:
        session = get_current_session()
        if session is None:
            raise RuntimeError("Not in tmux session and no session specified")

    # Smart split direction based on terminal dimensions
    # Default to stacked panes (-v) which works well on tablets/portrait
    # Only use side-by-side (-h) when terminal is very wide (ultrawide/landscape)
    width, height = _get_window_dimensions(session)
    # Use side-by-side only if width is >2.5x height (clearly ultrawide)
    split_flag = "-h" if width > height * 2.5 else "-v"

    # Find the last pane index to split FROM it (ensures new pane is appended, not inserted)
    # This prevents pane index shuffling when multiple workers are spawned
    panes = list_panes(session)
    last_pane_index = max(p.index for p in panes) if panes else 0

    # Build split-window command
    # -d: don't change focus to new pane
    # -P: print pane info
    # -F: format string
    split_cmd = [
        "tmux", "split-window",
        "-t", f"{session}.{last_pane_index}",  # split the LAST pane (append, don't insert)
        split_flag,  # smart split direction
        "-d",  # detached (don't steal focus)
        "-P", "-F", "#{pane_index}:#{pane_id}"  # return pane info
    ]

    if cwd:
        split_cmd.extend(["-c", cwd])

    result = run_command(split_cmd, timeout=10)

    if not result.success:
        raise RuntimeError(f"Failed to create pane: {result.stderr}")

    # Parse output: "1:%42"
    output = result.stdout.strip()
    pane_index = int(output.split(":")[0])

    # Wait for shell to be ready (race condition fix)
    time.sleep(0.4)

    # Send command if provided
    if cmd:
        send_to_pane(session, pane_index, cmd)

    # Apply main-top layout: orchestrator (pane 0) at top, workers below
    _apply_main_top_layout(session)

    return pane_index


def _apply_main_top_layout(session: str) -> None:
    """Apply main-horizontal layout: orchestrator on top, workers evenly tiled below.

    Uses tmux's built-in main-horizontal layout which:
    - Keeps pane 0 (orchestrator) as main pane at top (60% height)
    - Evenly tiles all worker panes (1+) in the bottom 40%
    - Maintains stable pane indices (new panes append, don't insert)

    Layout (2 panes):
        [   orchestrator   ]  <- pane 0, 60%
        [     worker 1     ]  <- pane 1, 40%

    Layout (3+ panes):
        [    orchestrator    ]  <- pane 0, 60%
        [ worker 1 ][ worker 2 ]  <- panes 1+, evenly split in 40%

    Args:
        session: Tmux session name.
    """
    panes = list_panes(session)
    if len(panes) <= 1:
        return  # No layout needed for single pane

    # Get window height for main pane size calculation
    result = run_command(
        ["tmux", "display", "-t", session, "-p", "#{window_height}"],
        timeout=5,
    )
    if not result.success:
        return

    window_height = int(result.stdout.strip())
    main_height = int(window_height * 0.6)  # Orchestrator gets 60%

    # Apply main-horizontal layout (main pane on top, others tiled below)
    # The main-pane-height option sets how much space the main pane gets
    run_command([
        "tmux", "select-layout", "-t", session, "main-horizontal"
    ], timeout=5)

    # Set the main pane height
    run_command([
        "tmux", "set-window-option", "-t", session, "main-pane-height", str(main_height)
    ], timeout=5)

    # Re-apply layout to pick up the new height
    run_command([
        "tmux", "select-layout", "-t", session, "main-horizontal"
    ], timeout=5)


def list_panes(session: str | None = None) -> list[PaneInfo]:
    """List all panes in a session.

    Args:
        session: Target session (default: auto-detect from $TMUX_PANE)

    Returns:
        List of PaneInfo objects for each pane.
    """
    if session is None:
        session = get_current_session()
        if session is None:
            raise RuntimeError("Not in tmux session and no session specified")

    result = run_command(
        [
            "tmux", "list-panes",
            "-t", session,
            "-F", "#{pane_index}:#{pane_id}:#{pane_pid}:#{pane_current_command}:#{pane_active}"
        ],
        timeout=5,
    )

    if not result.success:
        return []

    panes = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(":")
        if len(parts) >= 5:
            panes.append(PaneInfo(
                index=int(parts[0]),
                pane_id=parts[1],
                pid=int(parts[2]),
                command=parts[3],
                active=parts[4] == "1"
            ))

    return panes


def send_to_target(target: str, text: str, enter: bool = True) -> None:
    """Send text to a tmux target (session or `session.pane_index`).

    Single source of truth for "paste text into tmux + press Enter the right
    number of times". Both pane-scoped (`send_to_pane`) and session-scoped
    (`agentwire send -s ...`) callers route through here, so the paste
    threshold, the wait-before-Enter, and the bracketed-paste double-Enter
    behave identically everywhere.

    Behavior:
      - Non-trivial text (`\\n` or len > 10) is pasted via load-buffer +
        paste-buffer; short text uses `send-keys -l` (literal mode).
      - When `enter=True`, an Enter is sent after a 0.5–1.0s settle delay.
      - Multi-line / long prompts (`\\n` or len > 200) get a SECOND Enter
        after another 0.5s. Claude Code renders these as
        `[Pasted text +N lines]` and needs one Enter to dismiss the banner
        plus one to actually submit. Skipping the second leaves the prompt
        stuck in the input — the failure that hung the scheduler at 8am.
    """
    import tempfile

    use_buffer = "\n" in text or len(text) > 10
    needs_double_enter = "\n" in text or len(text) > 200

    if use_buffer:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(text)
            temp_path = f.name
        try:
            result = run_command(["tmux", "load-buffer", temp_path], timeout=5)
            if not result.success:
                raise RuntimeError(f"Failed to load buffer: {result.stderr.strip()}")
            # -p wraps the paste in bracketed-paste delimiters when the app has
            # requested them (Claude Code always does; tmux falls back to a
            # plain paste otherwise). Without the explicit end-of-paste marker,
            # Claude Code detects the paste heuristically as an input burst,
            # and an Enter arriving inside that window is coalesced INTO the
            # paste as a newline instead of submitting — the swallowed-Enter
            # root cause behind the #698 stress-test failures.
            result = run_command(["tmux", "paste-buffer", "-p", "-t", target], timeout=5)
            if not result.success:
                raise RuntimeError(f"Target '{target}' not found: {result.stderr.strip()}")
        finally:
            os.unlink(temp_path)
    else:
        result = run_command(["tmux", "send-keys", "-t", target, "-l", text], timeout=5)
        if not result.success:
            raise RuntimeError(f"Target '{target}' not found: {result.stderr.strip()}")

    if not enter:
        return

    time.sleep(1.0 if use_buffer else 0.5)
    run_command(["tmux", "send-keys", "-t", target, "Enter"], timeout=5)

    if needs_double_enter:
        time.sleep(0.5)
        run_command(["tmux", "send-keys", "-t", target, "Enter"], timeout=5)


def send_to_pane(session: str | None, pane_index: int, text: str, enter: bool = True) -> None:
    """Send text to a specific pane.

    Thin wrapper over :func:`send_to_target` that resolves the session and
    builds the `session.pane_index` target string. All paste / Enter
    behavior lives in `send_to_target` — keep changes there, not here.

    Args:
        session: Target session (default: auto-detect from $TMUX_PANE)
        pane_index: Target pane index
        text: Text to send
        enter: Whether to send Enter key after text
    """
    if session is None:
        session = get_current_session()
        if session is None:
            raise RuntimeError("Not in tmux session and no session specified")

    send_to_target(f"{session}.{pane_index}", text, enter=enter)


def capture_pane(
    session: str | None,
    pane_index: int,
    lines: int | None = None
) -> str:
    """Capture output from a specific pane.

    Args:
        session: Target session (default: auto-detect from $TMUX_PANE)
        pane_index: Target pane index
        lines: Number of lines to capture (default: all history)

    Returns:
        The captured pane content.
    """
    if session is None:
        session = get_current_session()
        if session is None:
            raise RuntimeError("Not in tmux session and no session specified")

    target = f"{session}.{pane_index}"
    cmd = ["tmux", "capture-pane", "-t", target, "-p"]

    if lines is None:
        # Capture full history
        cmd.extend(["-S", "-"])
    else:
        # Capture last N lines
        cmd.extend(["-S", f"-{lines}"])

    result = run_command(cmd, timeout=5)
    return result.stdout


def kill_pane(session: str | None, pane_index: int) -> None:
    """Kill a specific pane.

    Args:
        session: Target session (default: auto-detect from $TMUX_PANE)
        pane_index: Target pane index

    Raises:
        RuntimeError: If trying to kill pane 0 (orchestrator)
    """
    if pane_index == 0:
        raise RuntimeError("Cannot kill pane 0 (orchestrator)")

    if session is None:
        session = get_current_session()
        if session is None:
            raise RuntimeError("Not in tmux session and no session specified")

    target = f"{session}.{pane_index}"
    result = run_command(["tmux", "kill-pane", "-t", target], timeout=5)
    if not result.success:
        raise RuntimeError(f"Pane {pane_index} not found: {result.stderr.strip()}")


def focus_pane(session: str | None, pane_index: int) -> None:
    """Focus (jump to) a specific pane.

    Args:
        session: Target session (default: auto-detect from $TMUX_PANE)
        pane_index: Target pane index
    """
    if session is None:
        session = get_current_session()
        if session is None:
            raise RuntimeError("Not in tmux session and no session specified")

    target = f"{session}.{pane_index}"
    run_command(["tmux", "select-pane", "-t", target], timeout=5)


# === Worktree Support ===


@dataclass
class RepoInfo:
    """Information about a git repository."""
    root: str  # Absolute path to repo root
    name: str  # Repository name (directory name)
    current_branch: str  # Current branch name


def get_repo_info(cwd: str | None = None) -> RepoInfo | None:
    """Get git repository info from the current or specified directory.

    Args:
        cwd: Directory to check (default: current working directory)

    Returns:
        RepoInfo if in a git repo, None otherwise.
    """
    if cwd is None:
        cwd = os.getcwd()

    # Get repo root
    result = run_command(["git", "rev-parse", "--show-toplevel"], cwd=cwd, timeout=5)
    if not result.success:
        return None

    root = result.stdout.strip()
    name = os.path.basename(root)

    # Get current branch
    result = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, timeout=5)
    current_branch = result.stdout.strip() if result.success else "HEAD"

    return RepoInfo(root=root, name=name, current_branch=current_branch)


def create_worker_worktree(branch_name: str, cwd: str | None = None) -> str:
    """Create a git worktree for a worker branch.

    Creates a new branch from current HEAD and a worktree in a sibling directory.
    If the branch already exists, uses it. If the worktree already exists, returns its path.

    Args:
        branch_name: Name for the new branch
        cwd: Working directory (default: current)

    Returns:
        Absolute path to the worktree directory.

    Raises:
        RuntimeError: If not in a git repo or worktree creation fails.
    """
    repo = get_repo_info(cwd)
    if repo is None:
        raise RuntimeError("Not in a git repository")

    # Worktree location: sibling directory named {repo}-{branch}
    # e.g., /projects/agentwire-dev -> /projects/agentwire-dev-feature-x
    parent_dir = os.path.dirname(repo.root)
    worktree_path = os.path.join(parent_dir, f"{repo.name}-{branch_name}")

    # Check if worktree already exists
    if os.path.exists(worktree_path):
        # Verify it's a valid worktree
        result = run_command(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=worktree_path,
            timeout=5,
        )
        if result.success and result.stdout.strip() == "true":
            return worktree_path
        else:
            raise RuntimeError(f"Path exists but is not a git worktree: {worktree_path}")

    # Check if branch exists
    result = run_command(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo.root,
        timeout=5,
    )
    branch_exists = result.success

    if branch_exists:
        # Create worktree from existing branch
        result = run_command(
            ["git", "worktree", "add", worktree_path, branch_name],
            cwd=repo.root,
            timeout=30,
        )
    else:
        # Create new branch and worktree from current HEAD
        result = run_command(
            ["git", "worktree", "add", "-b", branch_name, worktree_path],
            cwd=repo.root,
            timeout=30,
        )

    if not result.success:
        raise RuntimeError(f"Failed to create worktree: {result.stderr}")

    return worktree_path
