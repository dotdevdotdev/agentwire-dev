#!/usr/bin/env python3
"""
AgentWire Damage Control Audit Logger
======================================
Logs all security decisions (blocked, asked, allowed) to JSONL files for analysis.

Storage: ~/.agentwire/logs/damage-control/YYYY-MM-DD.jsonl
Format: One JSON object per line (JSONL)

Fields:
- timestamp: ISO 8601 timestamp
- session_id: AgentWire session name (env override → tmux pane → #871 metadata)
- conversation_id: Claude conversation UUID (from the hook stdin payload)
- agent_id: Agent identifier (if in parallel execution)
- tool: Tool name (Bash, Edit, Write)
- command: Command/path that was checked
- decision: "blocked", "asked", "allowed"
- blocked_by: Pattern/rule that triggered block (if blocked)
- user_approved: Boolean (if asked pattern)
- pattern_matched: Regex pattern that matched
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

# The hook's parsed stdin payload. Each damage-control hook assigns this right
# after ``json.load(sys.stdin)`` so every log_* call in that process carries
# attribution without threading a kwarg through every call site. Claude Code
# puts the conversation UUID in the payload's ``session_id`` field — the same
# id agentwire mints and records per #871 — so this is the recorded identity,
# not a scrape.
HOOK_INPUT: dict = {}


def get_log_dir() -> Path:
    """Get or create the audit log directory."""
    agentwire_dir = os.environ.get("AGENTWIRE_DIR", os.path.expanduser("~/.agentwire"))
    log_dir = Path(agentwire_dir) / "logs" / "damage-control"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_log_file() -> Path:
    """Get today's log file path."""
    log_dir = get_log_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    return log_dir / f"{today}.jsonl"


def _conversation_id() -> Optional[str]:
    """Claude's conversation UUID for this hook invocation, or None.

    Comes from the hook stdin payload (``session_id`` there is the
    conversation id agentwire mints per #871).
    """
    cid = HOOK_INPUT.get("session_id") if isinstance(HOOK_INPUT, dict) else None
    return cid if isinstance(cid, str) and cid else None


def _tmux_session_name() -> Optional[str]:
    """The tmux session this hook process runs inside, or None.

    The hook inherits ``TMUX``/``TMUX_PANE`` from the pane's shell, so asking
    tmux for ``#S`` names the agentwire session directly (session names ARE
    tmux session names). Never raises; a couple-second timeout bounds the
    worst case.
    """
    if not os.environ.get("TMUX"):
        return None
    cmd = ["tmux", "display-message", "-p", "#S"]
    pane = os.environ.get("TMUX_PANE")
    if pane:
        cmd[2:2] = ["-t", pane]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=2, check=False,
        )
    except Exception:
        return None
    name = result.stdout.strip() if result.returncode == 0 else ""
    return name or None


def _sessions_dir() -> Path:
    """Mirror of ``core.sessions_dir()`` — the SSOT root for #871 records.

    This hook is a standalone PEP 723 script (uv isolated env) and cannot
    import ``agentwire.core``, so the join is mirrored here; the structural
    SSOT test exempts exactly this line.
    """
    agentwire_dir = os.environ.get("AGENTWIRE_DIR", os.path.expanduser("~/.agentwire"))
    return Path(agentwire_dir) / "sessions"


def _session_from_metadata(conversation_id: str) -> Optional[str]:
    """Resolve a session name by scanning #871's recorded launch metadata.

    ``~/.agentwire/sessions/<name>/metadata.json`` carries the
    ``conversation_ids`` chain; the entry containing this conversation id names
    the session. Best-effort — corrupt/missing records are skipped.
    """
    try:
        entries = list(_sessions_dir().iterdir())
    except OSError:
        return None
    for entry in entries:
        meta_file = entry / "metadata.json"
        try:
            metadata = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        chain = metadata.get("conversation_ids") or []
        if isinstance(chain, list) and conversation_id in chain:
            return entry.name
    return None


def get_session_context() -> dict:
    """Session attribution for an audit row (#940 prerequisite).

    ``session_id`` is the agentwire session NAME; ``conversation_id`` is the
    Claude conversation UUID from the hook payload. Resolution order for the
    name: explicit env override → tmux (the pane the hook runs in) → #871
    metadata scan by conversation id → ``"unknown"``.

    FAIL-OPEN by design: attribution must never block an action or crash the
    hook, so every probe swallows its own failures and the worst outcome is
    the pre-#940 row shape ("unknown").
    """
    conversation_id = None
    session = os.environ.get("AGENTWIRE_SESSION_ID") or None
    try:
        conversation_id = _conversation_id()
        if not session:
            session = _tmux_session_name()
        if not session and conversation_id:
            session = _session_from_metadata(conversation_id)
    except Exception:
        pass
    return {
        "session_id": session or "unknown",
        "conversation_id": conversation_id,
        "agent_id": os.environ.get("AGENTWIRE_AGENT_ID", "main"),
    }


def log_entry(
    tool: str,
    command: str,
    decision: str,
    blocked_by: Optional[str] = None,
    user_approved: Optional[bool] = None,
    pattern_matched: Optional[str] = None,
    rule_id: Optional[str] = None,
    escape_reason: Optional[str] = None,
    cwd: Optional[str] = None,
) -> None:
    """
    Write a log entry to the audit log.

    Args:
        tool: Tool name (Bash, Edit, Write)
        command: Command or path that was checked
        decision: "blocked", "asked", "allowed", "allowed_by_escape", or "allowed_by_disabled"
        blocked_by: Reason/pattern that triggered block
        user_approved: Whether user approved (for ask patterns)
        pattern_matched: The regex pattern that matched
        rule_id: Stable identifier of the matched rule (when applicable)
        escape_reason: Reason supplied via "# allow:" escape hatch (when applicable)
        cwd: Working directory where the command would have run
    """
    import os
    context = get_session_context()

    entry = {
        "timestamp": datetime.now().isoformat(),
        "session_id": context["session_id"],
        "conversation_id": context["conversation_id"],
        "agent_id": context["agent_id"],
        "tool": tool,
        "command": command,
        "decision": decision,
        "blocked_by": blocked_by,
        "user_approved": user_approved,
        "pattern_matched": pattern_matched,
        "rule_id": rule_id,
        "escape_reason": escape_reason,
        "cwd": cwd or os.environ.get("PWD") or os.getcwd(),
    }

    log_file = get_log_file()

    # Append to JSONL file
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_blocked(
    tool: str,
    command: str,
    reason: str,
    pattern: Optional[str] = None,
    rule_id: Optional[str] = None,
) -> None:
    """
    Log a blocked operation.

    Args:
        tool: Tool name (Bash, Edit, Write)
        command: Command or path that was blocked
        reason: Human-readable reason for block
        pattern: The regex pattern that matched
        rule_id: Stable identifier of the matched rule
    """
    log_entry(
        tool=tool,
        command=command,
        decision="blocked",
        blocked_by=reason,
        pattern_matched=pattern,
        rule_id=rule_id,
    )


def log_escape(
    tool: str,
    command: str,
    escape_reason: str,
) -> None:
    """
    Log a command that was allowed through the ``# allow: <reason>`` escape hatch.
    """
    log_entry(
        tool=tool,
        command=command,
        decision="allowed_by_escape",
        blocked_by=f"Escape hatch: {escape_reason}",
        escape_reason=escape_reason,
    )


def log_disabled(
    tool: str,
    command: str,
) -> None:
    """
    Log a command that was allowed because the safety system is disabled.
    """
    log_entry(
        tool=tool,
        command=command,
        decision="allowed_by_disabled",
        blocked_by="safety disabled",
    )


def log_asked(
    tool: str,
    command: str,
    reason: str,
    pattern: Optional[str] = None,
) -> None:
    """
    Log an operation that requires user confirmation.

    Note: This logs the ASK event. A subsequent log_allowed() or log_blocked()
    should be called after user responds.

    Args:
        tool: Tool name (Bash, Edit, Write)
        command: Command or path that requires confirmation
        reason: Human-readable reason for asking
        pattern: The regex pattern that matched
    """
    log_entry(
        tool=tool,
        command=command,
        decision="asked",
        blocked_by=reason,
        pattern_matched=pattern,
    )


def log_allowed(
    tool: str,
    command: str,
    user_approved: bool = False,
) -> None:
    """
    Log an allowed operation.

    Args:
        tool: Tool name (Bash, Edit, Write)
        command: Command or path that was allowed
        user_approved: Whether this was explicitly approved by user (for ask patterns)
    """
    log_entry(
        tool=tool,
        command=command,
        decision="allowed",
        user_approved=user_approved if user_approved else None,
    )


def log_user_approval(
    tool: str,
    command: str,
    approved: bool,
) -> None:
    """
    Log user's response to an ask pattern.

    Args:
        tool: Tool name (Bash, Edit, Write)
        command: Command or path that was asked about
        approved: Whether user approved (True) or rejected (False)
    """
    if approved:
        log_entry(
            tool=tool,
            command=command,
            decision="allowed",
            user_approved=True,
        )
    else:
        log_entry(
            tool=tool,
            command=command,
            decision="blocked",
            blocked_by="User rejected",
            user_approved=False,
        )


# CLI for testing
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python audit_logger.py <test|blocked|asked|allowed>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "test":
        # Write test entries
        log_blocked("Bash", "rm -rf /", "rm with recursive/force flags", r"\brm\s+-[rRf]")
        log_asked("Bash", "git checkout -- .", "Discards all uncommitted changes", r"\bgit\s+checkout\s+--\s*\.")
        log_allowed("Bash", "ls -la", user_approved=False)
        log_user_approval("Bash", "git branch -D old-feature", approved=True)
        print(f"✓ Test entries written to {get_log_file()}")

    elif cmd == "blocked":
        tool = sys.argv[2] if len(sys.argv) > 2 else "Bash"
        command = sys.argv[3] if len(sys.argv) > 3 else "test command"
        reason = sys.argv[4] if len(sys.argv) > 4 else "test reason"
        log_blocked(tool, command, reason)
        print("✓ Blocked entry logged")

    elif cmd == "asked":
        tool = sys.argv[2] if len(sys.argv) > 2 else "Bash"
        command = sys.argv[3] if len(sys.argv) > 3 else "test command"
        reason = sys.argv[4] if len(sys.argv) > 4 else "test reason"
        log_asked(tool, command, reason)
        print("✓ Asked entry logged")

    elif cmd == "allowed":
        tool = sys.argv[2] if len(sys.argv) > 2 else "Bash"
        command = sys.argv[3] if len(sys.argv) > 3 else "test command"
        log_allowed(tool, command)
        print("✓ Allowed entry logged")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
