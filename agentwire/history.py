"""
Claude Code conversation history utilities.

Reads conversation data from:
- ~/.claude/history.jsonl - user message history with timestamps and projects
- ~/.claude/projects/{encoded-path}/*.jsonl - session files with summaries

Supports both local and remote machines via SSH for distributed setups.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .ssh import ssh_base_opts
from .utils.file_io import load_json
from .utils.paths import agentwire_dir
from .utils.subprocess import run_command

# Claude Code data directories
CLAUDE_DIR = Path.home() / ".claude"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def resolve_session_id(prefix: str, machine: str = "local") -> str | None:
    """Resolve a session ID prefix to full UUID.

    Args:
        prefix: Session ID prefix (e.g., "b52e2fac" or full UUID)
        machine: Machine ID or 'local'

    Returns:
        Full session ID if unique match found, None otherwise.
    """
    # If it looks like a full UUID, return as-is
    if len(prefix) == 36 and prefix.count("-") == 4:
        return prefix

    # Search history for matching session IDs
    if machine == "local":
        history_path = str(HISTORY_FILE)
    else:
        history_path = "~/.claude/history.jsonl"

    content = _read_file_content(history_path, machine)
    if not content:
        return None

    matches = set()
    for line in content.strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
            session_id = entry.get("sessionId", "")
            if session_id.startswith(prefix):
                matches.add(session_id)
        except json.JSONDecodeError:
            continue

    # Return if exactly one match
    if len(matches) == 1:
        return matches.pop()

    return None


def encode_project_path(path: str) -> str:
    """Encode a cwd to the ``~/.claude/projects/<dir>`` name Claude Code uses.

    The rule is **every character outside ``[A-Za-z0-9]`` becomes ``-``**, one
    for one — nothing is dropped, collapsed, or case-folded.

    Derived EMPIRICALLY, not from documentation (#871), the same way #878 had
    to measure tmux's name mangling instead of guessing it:

    - 528 ground-truth pairs were read out of the local history — every
      ``*.jsonl`` records the ``cwd`` it was written from — and 527 matched
      this rule exactly. (The one holdout is a project directory renamed on
      disk after the fact, i.e. the very orphaning this module exists to
      repair, not a counter-example to the encoding.)
    - The remaining characters were swept through a real ``claude`` run:
      a directory segment ``a_b.c+d~e@f,g=h!i#j%k^l&m n o'p`` produced
      ``a-b-c-d-e-f-g-h-i-j-k-l-m-n-o-p``.
    - Non-ASCII was swept the same way: ``café-日本-Ωx`` produced
      ``caf------x``, so the class is ASCII ``[A-Za-z0-9]`` and **not**
      :meth:`str.isalnum`, which would have preserved ``é``/``日``/``Ω``.

    The previous implementation replaced only ``/``, which silently produced
    the wrong directory for any path containing a dot, an underscore, or a
    space — including ``~/.claude`` and ``~/.agentwire/council/<n>/workspace``,
    both of which really exist here. That is the same dot-shaped bug class as
    #865 → #868 → #870 → #878.

    There is deliberately no inverse. The mapping is many-to-one (``/``, ``.``
    and ``-`` all encode to ``-``), so a directory name cannot be decoded back
    to a cwd — it can only be compared against the encoding of a cwd you
    already know. Callers get that known cwd from ``cwd_at_launch`` in the
    session metadata (#881).
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


# The same rule, written in shell — expands to the history directory for the
# shell's OWN cwd (#901).
#
# The launch line stored in ``AGENTWIRE_LAUNCH_CMD`` exists to be re-run
# (#856/#866), so it has to decide ``--session-id`` vs ``--resume`` at SHELL
# runtime, which means this one encoding has to exist in shell as well as in
# Python. They are a pair: change :func:`encode_project_path` and change this.
#
# Two details are measured, not assumed:
#
# - ``pwd -P``, not ``$PWD``: Claude keys history by the PHYSICAL cwd.
#   Launching from ``/private/tmp/awsym/link -> …/real`` wrote the transcript
#   under ``-private-tmp-awsym-real``, so ``$PWD`` (which keeps the symlink as
#   typed) would look in a directory that never exists. On macOS every
#   ``/tmp/...`` path is exactly that symlink.
# - ``LC_ALL=C``, so ``A-Za-z0-9`` means ASCII whatever the operator's locale
#   collates it to.
HISTORY_DIR_SHELL = (
    "\"$HOME/.claude/projects/$(pwd -P | LC_ALL=C sed 's/[^A-Za-z0-9]/-/g')\""
)


def history_key_sources(cwd: str | Path) -> list[str]:
    """The cwd spellings a transcript might be keyed under, best first.

    Normally there is exactly one. There are two when the recorded path and
    its symlink-resolved form differ — the ``/tmp`` vs ``/private/tmp`` split
    on macOS being the case that actually bites, since ``cwd_at_launch`` is
    recorded verbatim from the caller while Claude Code keys off the physical
    path its process reports. Checking both is the difference between finding
    the transcript and reporting a false "absent".
    """
    raw = str(cwd)
    out = [raw]
    try:
        resolved = str(Path(raw).expanduser().resolve())
    except OSError:
        resolved = raw
    if resolved != raw:
        out.append(resolved)
    return out


def history_key_candidates(cwd: str | Path) -> list[str]:
    """:func:`history_key_sources`, encoded — the directory names to check."""
    return [encode_project_path(p) for p in history_key_sources(cwd)]


#: Record types that make a transcript a CONVERSATION rather than a stub.
#: See :func:`holds_a_conversation`.
_TURN_TYPES = {"user", "assistant"}


def holds_a_conversation(transcript: Path) -> bool:
    """Whether a ``.jsonl`` holds actual turns, not just session metadata.

    The file EXISTING is not enough, which is measured rather than assumed.
    Restarting a session whose history had been moved away left a 5-line file
    at the new key — ``last-prompt``, ``ai-title``, ``mode``,
    ``permission-mode``, ``file-history-snapshot`` — written by Claude as a
    side effect, with the real conversation still stranded under the old key.
    ``claude --resume`` on that id answered ``No conversation found with
    session ID``, so the file that looked like a hit was one claude refuses.

    That mattered concretely: ``restart`` would have passed the id to
    ``--resume``, claude would have refused to start, and the pane would have
    dropped to a bare shell — while ``doctor`` reported the orphan as healed.
    A conversation always carries at least one ``user``/``assistant`` record;
    the stub carries none, which is the whole difference.

    Streams and stops at the first turn, so the cost is a few lines rather
    than the file. Unreadable reads as "no conversation": the safe direction
    is to start fresh with the role intact, never to hand claude an id it will
    reject.
    """
    try:
        with transcript.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("type") in _TURN_TYPES:
                    return True
    except OSError:
        return False
    return False


@dataclass(frozen=True)
class ConversationLocation:
    """Where a recorded conversation's history actually is — or isn't (#871).

    A RECORDED conversation id does not guarantee a RESUMABLE conversation.
    ``conversation_ids`` records what agentwire *launched*; whether Claude
    still *has* it is a separate question, and it reduces to one predicate:

        ``resumable(id, cwd) == exists(<encoded_cwd>/<id>.jsonl)``

    — with one measured refinement: the file must hold actual turns, not
    just session metadata (:func:`holds_a_conversation`).

    That one file governs both directions of the flag pair, which is why this
    is the only probe either caller needs: ``--resume`` finds the conversation
    iff the file is there, and ``--session-id`` rejects an id as "already in
    use" iff the file is there (re-passing an id whose session never took a
    turn is *accepted*, because nothing was ever written).

    Three answers, and anything resuming from the record handles all three:

    - ``resumable`` — the ``.jsonl`` is under the key *cwd* implies, which is
      the only place ``claude --resume`` looks from that directory.
    - ``orphaned`` — it exists, but under a DIFFERENT cwd key. A moved
      worktree does this: the conversation is intact and unreachable at once.
      Recoverable by migrating the history dir.
    - ``gone`` — no history anywhere. Two ordinary ways to get here: a session
      launched but never prompted (the ``.jsonl`` is created lazily on the
      first turn, so it simply never existed), and Claude evicting it —
      ``~/.claude/projects/`` entries were observed dropping 563 -> 544 in
      ~25min with ``cleanupPeriodDays`` unset, cause not attributable. Treat
      history as a cache Claude owns.
    """

    conversation_id: str
    cwd: str
    expected_dir: Path
    found_at: Path | None
    elsewhere: tuple[Path, ...]

    @property
    def status(self) -> str:
        if self.found_at is not None:
            return "resumable"
        if self.elsewhere:
            return "orphaned"
        return "gone"

    @property
    def resumable(self) -> bool:
        return self.found_at is not None


def locate_conversation(
    conversation_id: str, cwd, projects_dir: Path | None = None
) -> ConversationLocation:
    """Locate *conversation_id*'s history relative to the cwd it launched in.

    The one probe ``agentwire restart``, ``agentwire doctor`` and
    ``history_migrate.resumable`` all use, so "is this resumable?" has a
    single answer rather than three nearly-identical ones that can disagree.
    Local only: it reads this machine's ``~/.claude/projects``.

    Every spelling in :func:`history_key_candidates` counts as "the expected
    place" — a symlinked cwd is one directory under two names, not an orphan.
    The scan for a stray copy runs ONLY when none of them has the file, so the
    common case costs one ``stat`` instead of one per project directory.
    """
    base = projects_dir or PROJECTS_DIR
    keys = history_key_candidates(cwd)
    expected_dirs = [base / key for key in keys]
    for expected_dir in expected_dirs:
        expected = expected_dir / f"{conversation_id}.jsonl"
        if expected.exists() and holds_a_conversation(expected):
            return ConversationLocation(
                conversation_id=conversation_id, cwd=str(cwd),
                expected_dir=expected_dirs[0], found_at=expected, elsewhere=(),
            )

    elsewhere: list[Path] = []
    try:
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d in expected_dirs:
                continue
            candidate = d / f"{conversation_id}.jsonl"
            if candidate.exists() and holds_a_conversation(candidate):
                elsewhere.append(candidate)
    except OSError:
        pass

    return ConversationLocation(
        conversation_id=conversation_id, cwd=str(cwd),
        expected_dir=expected_dirs[0], found_at=None, elsewhere=tuple(elsewhere),
    )


def _get_machine_config(machine_id: str) -> dict | None:
    """Load machine config from machines.json.

    Args:
        machine_id: Machine identifier.

    Returns:
        Machine config dict or None if not found.
    """
    machines_file = agentwire_dir() / "machines.json"
    machines_data = load_json(machines_file, default={})

    machines = machines_data.get("machines", [])
    for m in machines:
        if m.get("id") == machine_id:
            return m

    return None


def _run_ssh_command(machine: dict, command: str, timeout: int = 10) -> tuple[bool, str]:
    """Run command on remote machine via SSH.

    Args:
        machine: Machine config dict with host, user, port
        command: Shell command to run
        timeout: Command timeout in seconds

    Returns:
        (success, output) tuple
    """
    host = machine.get("host", machine.get("id", ""))
    user = machine.get("user")
    port = machine.get("port")

    # Build SSH target
    if user:
        ssh_target = f"{user}@{host}"
    else:
        ssh_target = host

    # Build SSH command with connection timeout
    ssh_cmd = ["ssh", *ssh_base_opts(), "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
    if port:
        ssh_cmd.extend(["-p", str(port)])
    ssh_cmd.extend([ssh_target, command])

    result = run_command(ssh_cmd, timeout=timeout)
    return result.success, result.stdout


def _read_file_content(filepath: str, machine: str = "local") -> str | None:
    """Read file content, local or remote.

    Args:
        filepath: Path to file
        machine: Machine ID or 'local'

    Returns:
        File content as string, or None if not found/error
    """
    if machine == "local":
        path = Path(filepath)
        if not path.exists():
            return None
        try:
            return path.read_text()
        except IOError:
            return None
    else:
        machine_config = _get_machine_config(machine)
        if not machine_config:
            return None
        success, output = _run_ssh_command(machine_config, f"cat '{filepath}' 2>/dev/null")
        return output if success else None


def _list_directory(dirpath: str, machine: str = "local") -> list[str]:
    """List files in directory, local or remote.

    Args:
        dirpath: Path to directory
        machine: Machine ID or 'local'

    Returns:
        List of filenames (not full paths)
    """
    if machine == "local":
        path = Path(dirpath)
        if not path.exists() or not path.is_dir():
            return []
        return [f.name for f in path.iterdir() if f.is_file()]
    else:
        machine_config = _get_machine_config(machine)
        if not machine_config:
            return []
        success, output = _run_ssh_command(
            machine_config, f"ls -1 '{dirpath}' 2>/dev/null", timeout=15
        )
        if not success:
            return []
        return [f for f in output.strip().split("\n") if f]


def _grep_file(filepath: str, pattern: str, machine: str = "local") -> list[str]:
    """Grep lines matching pattern from file.

    Args:
        filepath: Path to file
        pattern: grep pattern to match
        machine: Machine ID or 'local'

    Returns:
        List of matching lines
    """
    if machine == "local":
        path = Path(filepath)
        if not path.exists():
            return []
        result = run_command(["grep", "-E", pattern, str(path)], timeout=5)
        return [line for line in result.stdout.strip().split("\n") if line]
    else:
        machine_config = _get_machine_config(machine)
        if not machine_config:
            return []
        success, output = _run_ssh_command(
            machine_config, f"grep -E '{pattern}' '{filepath}' 2>/dev/null", timeout=10
        )
        if not success:
            return []
        return [line for line in output.strip().split("\n") if line]


def get_history(project_path: str, machine: str = "local", limit: int = 20) -> list[dict]:
    """Get conversation history for a project.

    Reads from ~/.claude/history.jsonl and enriches with summaries from session files.

    Args:
        project_path: Absolute path to project directory
        machine: Machine ID or 'local'
        limit: Maximum number of sessions to return

    Returns:
        List of session dicts: {sessionId, firstMessage, lastSummary, timestamp, messageCount}
        Sorted by timestamp descending (newest first).
    """
    # Determine paths based on machine
    if machine == "local":
        history_path = str(HISTORY_FILE)
        projects_base = str(PROJECTS_DIR)
    else:
        history_path = "~/.claude/history.jsonl"
        projects_base = "~/.claude/projects"

    # Read history.jsonl
    content = _read_file_content(history_path, machine)
    if not content:
        return []

    # Parse and filter by project
    sessions: dict[str, dict] = {}  # sessionId -> session data

    for line in content.strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Filter by project path
        if entry.get("project") != project_path:
            continue

        session_id = entry.get("sessionId")
        if not session_id:
            continue

        timestamp = entry.get("timestamp", 0)
        display = entry.get("display", "")

        if session_id not in sessions:
            sessions[session_id] = {
                "sessionId": session_id,
                "firstMessage": display,
                "lastSummary": None,
                "timestamp": timestamp,
                "messageCount": 1,
            }
        else:
            sessions[session_id]["messageCount"] += 1
            # Update timestamp if newer
            if timestamp > sessions[session_id]["timestamp"]:
                sessions[session_id]["timestamp"] = timestamp

    # Get summaries from session files
    encoded_path = encode_project_path(project_path)
    session_dir = f"{projects_base}/{encoded_path}"

    for session_id in sessions:
        session_file = f"{session_dir}/{session_id}.jsonl"
        # Grep for summary lines only - more efficient than full parse
        summary_lines = _grep_file(session_file, '"type":"summary"', machine)
        if summary_lines:
            # Get the last summary
            try:
                last_summary = json.loads(summary_lines[-1])
                sessions[session_id]["lastSummary"] = last_summary.get("summary")
            except json.JSONDecodeError:
                pass

    # Sort by timestamp descending and limit
    result = sorted(sessions.values(), key=lambda x: x["timestamp"], reverse=True)
    return result[:limit]


def get_session_detail(session_id: str, machine: str = "local") -> dict | None:
    """Get full details for a specific session.

    Args:
        session_id: UUID of the session (or unique prefix)
        machine: Machine ID or 'local'

    Returns:
        Session dict: {sessionId, summaries, firstMessage, timestamps: {start, end}, gitBranch, messageCount}
        None if session not found.
    """
    # Resolve prefix to full session ID
    resolved = resolve_session_id(session_id, machine)
    if resolved:
        session_id = resolved

    # First find the session in history to get project path
    if machine == "local":
        history_path = str(HISTORY_FILE)
        projects_base = str(PROJECTS_DIR)
    else:
        history_path = "~/.claude/history.jsonl"
        projects_base = "~/.claude/projects"

    content = _read_file_content(history_path, machine)
    if not content:
        return None

    # Find messages for this session
    messages = []
    project_path = None

    for line in content.strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("sessionId") == session_id:
            messages.append(entry)
            if not project_path:
                project_path = entry.get("project")

    if not messages or not project_path:
        return None

    # Sort messages by timestamp
    messages.sort(key=lambda x: x.get("timestamp", 0))

    # Get session file for summaries and git branch
    encoded_path = encode_project_path(project_path)
    session_file = f"{projects_base}/{encoded_path}/{session_id}.jsonl"

    summaries = []
    git_branch = None

    # Grep for summary lines
    summary_lines = _grep_file(session_file, '"type":"summary"', machine)
    for line in summary_lines:
        try:
            entry = json.loads(line)
            if entry.get("summary"):
                summaries.append(entry.get("summary"))
        except json.JSONDecodeError:
            pass

    # Try to get git branch from first file-history-snapshot entry
    # This is a simple grep - we just look for gitBranch in any line
    branch_lines = _grep_file(session_file, '"gitBranch"', machine)
    if branch_lines:
        try:
            # Parse the first entry that has gitBranch
            for line in branch_lines:
                entry = json.loads(line)
                if "gitBranch" in str(entry):
                    # Could be nested in various places
                    if isinstance(entry.get("gitBranch"), str):
                        git_branch = entry["gitBranch"]
                        break
        except json.JSONDecodeError:
            pass

    return {
        "sessionId": session_id,
        "summaries": summaries,
        "firstMessage": messages[0].get("display", "") if messages else None,
        "timestamps": {
            "start": messages[0].get("timestamp") if messages else None,
            "end": messages[-1].get("timestamp") if messages else None,
        },
        "gitBranch": git_branch,
        "messageCount": len(messages),
    }
