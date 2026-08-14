"""Safety CLI commands for AgentWire damage control integration.

This module is the CLI front end. All decision logic — pattern matching,
allowlist evaluation, the decision ladder — lives in ``agentwire.safety._core``,
which is also inlined into the bundled hook scripts. See #164 for the dedup
history and ``scripts/regen_damage_control_hooks.py`` for how the hook scripts
stay in sync.
"""

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None

# Re-exported from safety._core so callers and tests can import the whole
# damage-control surface from agentwire.safety_commands.
from agentwire.safety._core import (  # noqa: F401
    ALL_OPERATIONS,
    NO_DELETE_BLOCKED,
    READ_ONLY_BLOCKED,
    _extract_command_paths,
    _find_project_config,
    _infer_operation_from_reason,
    _parse_allowed_entry,
    check_command,
    check_path,
    check_path_patterns,
    glob_to_regex,
    is_command_path_allowed,
    is_glob_pattern,
    is_path_allowed_for_op,
    load_allowed_paths,
    load_config,
    match_path,
    matches_path_in_command,
)

# Default config directory
CONFIG_DIR = Path.home() / ".agentwire"
HOOKS_DIR = CONFIG_DIR / "hooks" / "damage-control"
LOGS_DIR = CONFIG_DIR / "logs" / "damage-control"
RULES_DIR = CONFIG_DIR / "damage-control"
TOOLDEFS_DIR = CONFIG_DIR / "tooldefs"
# Host-owned, agent-unwritable policy file: kill switch + rule knobs (#466).
DAMAGECONTROL_FILE = CONFIG_DIR / "damagecontrol.yml"

DAMAGECONTROL_SCAFFOLD = """\
# AgentWire damage-control policy (#466) — HOST-OWNED, agent-unwritable.
#
# This file (and project-root `.damagecontrol.yml`) are the ONLY place the
# damage-control kill switch and rule knobs live. They are part of the protected
# control plane: the policed agent cannot write them (the `# allow:` escape
# hatch and the kill switch itself cannot override that), so an agent can never
# expand its own freedom. Loosening is always a host-side edit of this file.

# Master switch. false turns ALL command/path/outbound gating off. Missing file
# or missing key ⇒ true (fail-secure).
enabled: true

# Stable rule IDs to disable (see `agentwire safety status` / the portal Safety
# window for IDs). e.g.:
# disabled_rules:
#   - git.push
disabled_rules: []

# Rule IDs an UNATTENDED (scheduler) run may resolve `ask` → allow for, on top of
# the built-in default allowlist. e.g.:
# unattended_allow:
#   - gh.pr-merge
unattended_allow: []

# Per-project/global path allowlist — the human opt-in that re-permits paths
# (including protected control-plane files). This lives HERE, not in the
# agent-writable .agentwire.yml, because it overrides the protected check. e.g.:
# allowed_paths:
#   - path: "dist/*"
#     allow: all
#   - path: ".env.development"
#     allow: [read, write, edit]
allowed_paths: []
"""


def scaffold_damagecontrol_file() -> bool:
    """Create ~/.agentwire/damagecontrol.yml with `enabled: true` if missing.

    Returns True if it wrote the file, False if it already existed. Never
    overwrites an existing policy file.
    """
    if DAMAGECONTROL_FILE.exists():
        return False
    DAMAGECONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    DAMAGECONTROL_FILE.write_text(DAMAGECONTROL_SCAFFOLD)
    return True

# Files to install from the package (hook scripts only, not rules)
DAMAGE_CONTROL_FILES = [
    "bash-tool-damage-control.py",
    "edit-tool-damage-control.py",
    "write-tool-damage-control.py",
    "read-tool-damage-control.py",
    "mcp-tool-damage-control.py",
    "audit_logger.py",
]


def get_damage_control_source() -> Path:
    """Get path to the bundled rules directory."""
    package_dir = Path(__file__).parent
    source_dir = package_dir / "hooks" / "damage-control" / "rules"
    if source_dir.exists():
        return source_dir
    raise FileNotFoundError("Could not find damage-control rules in package")


def get_tooldefs_source() -> Path:
    """Get path to bundled tooldefs directory."""
    package_dir = Path(__file__).parent
    source_dir = package_dir / "tooldefs"
    if source_dir.exists():
        return source_dir
    raise FileNotFoundError("Could not find tooldefs in package")


def _bundled_rules_dir() -> Optional[Path]:
    """Return the bundled rules dir if no user override exists."""
    package_dir = Path(__file__).parent
    bundled = package_dir / "hooks" / "damage-control" / "rules"
    return bundled if bundled.exists() else None


def _resolve_rules_dir() -> Path:
    """User override (~/.agentwire/damage-control/) wins; else bundled rules/."""
    if RULES_DIR.exists() and any(RULES_DIR.glob("*.yaml")):
        return RULES_DIR
    bundled = _bundled_rules_dir()
    return bundled if bundled is not None else RULES_DIR


def _resolve_tooldefs_dir() -> Optional[Path]:
    """User tooldefs (~/.agentwire/tooldefs/) win; else bundled tooldefs."""
    if TOOLDEFS_DIR.exists() and any(TOOLDEFS_DIR.glob("*.yaml")):
        return TOOLDEFS_DIR
    try:
        return get_tooldefs_source()
    except FileNotFoundError:
        return None


def load_patterns() -> Dict[str, Any]:
    """Load merged patterns from rules + tooldef ask-patterns.

    Thin wrapper around ``_core.load_config`` that resolves the user/bundled
    rules and tooldefs directories from the cli-side path constants.
    """
    if not yaml:
        print("Error: PyYAML not installed.", file=sys.stderr)
        return {}
    rules_dir = _resolve_rules_dir()
    if not rules_dir.exists():
        return {}
    return load_config(rules_dir, _resolve_tooldefs_dir())


def check_command_safety(command: str, verbose: bool = False) -> Dict[str, Any]:
    """Dry-run check whether a command would be blocked/allowed/asked.

    Returns ``{decision, reason, pattern, command}``. Public API; preserved for
    backwards compatibility with existing callers and tests.
    """
    config = load_patterns()
    return check_command(command, config)


def get_safety_status() -> Dict[str, Any]:
    """Get current safety status — pattern counts, recent blocks, etc."""
    patterns = load_patterns()

    rule_files = sorted(f.name for f in RULES_DIR.glob("*.yaml")) if RULES_DIR.exists() else []
    tooldefs_count = len(list(TOOLDEFS_DIR.glob("*.yaml"))) if TOOLDEFS_DIR.exists() else 0

    from agentwire.safety._core import load_safety_config
    safety_cfg = load_safety_config()

    status: Dict[str, Any] = {
        "hooks_installed": HOOKS_DIR.exists(),
        "enabled": safety_cfg.get("enabled", True),
        "disabled_rules": list(safety_cfg.get("disabled_rules", []) or []),
        "policy_file": str(DAMAGECONTROL_FILE),
        "policy_file_exists": DAMAGECONTROL_FILE.exists(),
        "rules_dir": str(RULES_DIR),
        "patterns_exist": RULES_DIR.exists() and any(RULES_DIR.glob("*.yaml")),
        "rule_files": rule_files,
        "logs_dir": str(LOGS_DIR),
        "logs_exist": LOGS_DIR.exists(),
        "tooldefs_dir": str(TOOLDEFS_DIR),
        "tooldefs_count": tooldefs_count,
        # Two live rules sharing one id makes `disabled_rules` /
        # `unattended_allow` ambiguous, and is the only visible symptom of a
        # half-applied rule sync (#916).
        "duplicate_rule_ids": list(patterns.get("_duplicate_rule_ids") or []),
        "pattern_counts": {
            "bash_patterns": len(patterns.get("bashToolPatterns", [])),
            "zero_access_paths": len(patterns.get("zeroAccessPaths", [])),
            "read_only_paths": len(patterns.get("readOnlyPaths", [])),
            "no_delete_paths": len(patterns.get("noDeletePaths", [])),
            "allowed_paths": len(load_allowed_paths(patterns)),
        },
        "recent_blocks": [],
    }

    if LOGS_DIR.exists():
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = LOGS_DIR / f"{today}.jsonl"
        if log_file.exists():
            try:
                blocks = []
                with open(log_file, "r") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            if entry.get("decision") == "blocked":
                                blocks.append(entry)
                        except json.JSONDecodeError:
                            continue
                status["recent_blocks"] = blocks[-5:]
            except Exception as e:
                status["error"] = f"Error reading logs: {e}"

    return status


def query_audit_logs(
    tail: Optional[int] = None,
    session: Optional[str] = None,
    today: bool = False,
    pattern: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query audit logs with filters."""
    if not LOGS_DIR.exists():
        return []

    entries: List[Dict[str, Any]] = []

    if today:
        log_files = [LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"]
    else:
        # Sort chronologically (oldest first) so that within the merged list,
        # entries are strictly in time order. Callers that want newest-first
        # (e.g. the portal Safety section) reverse on their end after slicing.
        log_files = sorted(LOGS_DIR.glob("*.jsonl"))

    for log_file in log_files:
        if not log_file.exists():
            continue
        try:
            with open(log_file, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if session and entry.get("session_id") != session:
                            continue
                        if pattern:
                            cmd = entry.get("command", "")
                            blocked_by = entry.get("blocked_by", "")
                            if (
                                pattern.lower() not in cmd.lower()
                                and pattern.lower() not in blocked_by.lower()
                            ):
                                continue
                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue

    if tail:
        entries = entries[-tail:]
    return entries


def format_safety_status(status: Dict[str, Any]) -> str:
    """Format safety status for display."""
    lines = []
    lines.append("AgentWire Safety Status")
    lines.append("=" * 50)
    lines.append("")

    if not status["hooks_installed"]:
        lines.append("⚠️  Hooks not installed")
        lines.append("   Run: agentwire safety install")
        return "\n".join(lines)

    enabled = status.get("enabled", True)
    lines.append(f"{'✓' if enabled else '⚠️ '} Damage control: {'ENABLED' if enabled else 'DISABLED'}")
    lines.append(f"  Policy file: {status.get('policy_file', '')}"
                 f"{'' if status.get('policy_file_exists') else ' (missing → defaults to enabled)'}")
    if status.get("disabled_rules"):
        lines.append(f"  Disabled rules: {', '.join(status['disabled_rules'])}")
    lines.append("")

    lines.append(f"✓ Hooks directory: {status['hooks_installed']}")
    lines.append(f"✓ Rules directory: {status['rules_dir']}")
    lines.append(f"  Exists: {status['patterns_exist']}")
    if status.get("rule_files"):
        lines.append(f"  Files: {', '.join(status['rule_files'])}")
    lines.append("")

    lines.append("Pattern Counts:")
    for name, count in status["pattern_counts"].items():
        lines.append(f"  • {name.replace('_', ' ').title()}: {count}")
    lines.append("")

    lines.append(f"Audit Logs: {status['logs_dir']}")
    lines.append(f"  Exists: {status['logs_exist']}")
    lines.append("")

    if "tooldefs_dir" in status:
        lines.append(f"Tooldefs: {status['tooldefs_dir']}")
        lines.append(f"  Installed: {status['tooldefs_count']} tool definitions")
        lines.append("")

    if status["recent_blocks"]:
        lines.append(f"Recent Blocks (last {len(status['recent_blocks'])}):")
        for block in status["recent_blocks"]:
            timestamp = block.get("timestamp", "unknown")
            cmd = block.get("command", "unknown")[:60]
            reason = block.get("blocked_by", "unknown")[:50]
            lines.append(f"  [{timestamp}] {cmd}")
            lines.append(f"    → {reason}")
        lines.append("")
    else:
        lines.append("No recent blocks found.")
        lines.append("")

    return "\n".join(lines)


def format_check_result(result: Dict[str, Any]) -> str:
    """Format check result for display."""
    decision = result["decision"]
    if decision == "allow":
        icon, color = "✓", "\033[32m"
    elif decision == "block":
        icon, color = "✗", "\033[31m"
    else:
        icon, color = "?", "\033[33m"
    reset = "\033[0m"

    lines = [f"{color}{icon} Decision: {decision.upper()}{reset}"]
    lines.append(f"  Reason: {result['reason']}")
    if result.get("pattern"):
        lines.append(f"  Pattern: {result['pattern']}")
    lines.append(f"  Command: {result['command']}")
    return "\n".join(lines)


def format_audit_logs(entries: List[Dict[str, Any]]) -> str:
    """Format audit log entries for display."""
    if not entries:
        return "No audit log entries found."

    lines = []
    lines.append(f"Audit Logs ({len(entries)} entries)")
    lines.append("=" * 80)
    lines.append("")

    for entry in entries:
        timestamp = entry.get("timestamp", "unknown")
        session = entry.get("session_id", "unknown")
        tool = entry.get("tool", "unknown")
        cmd = entry.get("command", "unknown")
        decision = entry.get("decision", "unknown")
        blocked_by = entry.get("blocked_by", "")

        if decision == "blocked":
            color = "\033[31m"
        elif decision == "asked":
            color = "\033[33m"
        else:
            color = "\033[32m"
        reset = "\033[0m"

        lines.append(f"[{timestamp}] {color}{decision.upper()}{reset}")
        lines.append(f"  Session: {session}")
        lines.append(f"  Tool: {tool}")
        lines.append(f"  Command: {cmd[:100]}")
        if blocked_by:
            lines.append(f"  Blocked by: {blocked_by[:80]}")
        lines.append("")

    return "\n".join(lines)


def safety_check_cmd(command: str, verbose: bool = False) -> int:
    """CLI command: ``agentwire safety check``."""
    result = check_command_safety(command, verbose)
    print(format_check_result(result))
    return 0 if result["decision"] == "allow" else 1


def safety_status_cmd() -> int:
    """CLI command: ``agentwire safety status``."""
    print(format_safety_status(get_safety_status()))
    return 0


def safety_notify_unattended_block_cmd(
    reason: str, rule_id: str, command: str
) -> int:
    """CLI command: ``agentwire safety notify-unattended-block``.

    Invoked fire-and-forget by the damage-control hooks when an unattended
    (scheduler) run hits an ``ask``-tier command that isn't on the allowlist.

    This used to email the owner **per block**, immediately, with no throttle
    and no dedup: 96 emails over 14 days and accelerating, 54% of them the same
    rule and most of those the same rule in the same session, looping (#925).
    It now spools into :mod:`agentwire.safety_notify`, which digests and
    throttles on the pattern ``auth_expired`` and the dead-letter escalation
    already use.

    Note what is NOT here: the audit log. The hook writes it via ``log_blocked``
    on its own path *before* invoking this, so no throttling decision below can
    make a block go unrecorded — the digest points at ``agentwire safety logs``
    precisely because that record stays complete.
    """
    session = os.environ.get("AGENTWIRE_SESSION_NAME") or os.environ.get(
        "AGENTWIRE_SESSION_ID", "unknown"
    )
    from agentwire import safety_notify

    result = safety_notify.record_block(
        rule_id=rule_id, session=session, reason=reason, command=command
    )
    if not result["spooled"]:
        print("notify-unattended-block: could not spool the block", file=sys.stderr)
        return 1
    return 0


def safety_logs_cmd(
    tail: Optional[int] = None,
    session: Optional[str] = None,
    today: bool = False,
    pattern: Optional[str] = None,
) -> int:
    """CLI command: ``agentwire safety logs``."""
    print(format_audit_logs(query_audit_logs(tail, session, today, pattern)))
    return 0


def summarize_audit_blocks(days: int = 14) -> Dict[str, Any]:
    """Aggregate blocked audit rows over a window — the #940 measurement.

    Reads ``~/.agentwire/logs/damage-control/*.jsonl`` for the last *days*
    daily files (filename-dated, so no per-row timestamp parsing) and counts
    blocks by tier (attended vs unattended — ``blocked_by`` starting with
    ``"unattended"``, the same discriminator #940 used), by rule id, and by
    session. Also reports attribution coverage: rows written before the #940
    prerequisite carry ``session_id: "unknown"`` and no ``conversation_id``,
    and any error-rate analysis needs to know how much of the window that is.
    """
    summary: Dict[str, Any] = {
        "days": days,
        "total_blocks": 0,
        "attended": 0,
        "unattended": 0,
        "by_rule": {},
        "by_session": {},
        "by_tool": {},
        "attributed": 0,
        "unattributed": 0,
        "files_read": 0,
    }
    if not LOGS_DIR.exists():
        return summary

    from datetime import timedelta

    cutoff = datetime.now().date() - timedelta(days=days - 1)
    for log_file in sorted(LOGS_DIR.glob("*.jsonl")):
        try:
            file_date = datetime.strptime(log_file.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        try:
            lines = log_file.read_text().splitlines()
        except OSError:
            continue
        summary["files_read"] += 1
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("decision") != "blocked":
                continue
            summary["total_blocks"] += 1
            blocked_by = entry.get("blocked_by") or ""
            tier = "unattended" if blocked_by.startswith("unattended") else "attended"
            summary[tier] += 1
            rule = entry.get("rule_id") or "(no rule_id recorded)"
            summary["by_rule"][rule] = summary["by_rule"].get(rule, 0) + 1
            tool = entry.get("tool") or "unknown"
            summary["by_tool"][tool] = summary["by_tool"].get(tool, 0) + 1
            session = entry.get("session_id") or "unknown"
            if session == "unknown" and not entry.get("conversation_id"):
                summary["unattributed"] += 1
            else:
                summary["attributed"] += 1
            per = summary["by_session"].setdefault(
                session, {"attended": 0, "unattended": 0}
            )
            per[tier] += 1
    return summary


def format_safety_report(summary: Dict[str, Any]) -> str:
    """Human rendering of :func:`summarize_audit_blocks`."""
    lines = [
        f"Damage-control blocks — last {summary['days']} days "
        f"({summary['files_read']} log files)",
        "=" * 60,
        f"total blocks : {summary['total_blocks']}",
        f"  attended   : {summary['attended']}",
        f"  unattended : {summary['unattended']}",
        f"attribution  : {summary['attributed']} attributed, "
        f"{summary['unattributed']} unattributed (pre-#940 rows)",
    ]
    if summary["by_rule"]:
        lines += ["", "By rule:"]
        for rule, count in sorted(summary["by_rule"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:6d}  {rule}")
    if summary["by_tool"]:
        lines += ["", "By tool:"]
        for tool, count in sorted(summary["by_tool"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:6d}  {tool}")
    if summary["by_session"]:
        lines += ["", "By session (attended / unattended):"]
        ranked = sorted(
            summary["by_session"].items(),
            key=lambda kv: -(kv[1]["attended"] + kv[1]["unattended"]),
        )
        for session, per in ranked:
            lines.append(
                f"  {per['attended'] + per['unattended']:6d}  {session}"
                f"  ({per['attended']} / {per['unattended']})"
            )
    return "\n".join(lines)


def safety_report_cmd(days: int = 14, json_output: bool = False) -> int:
    """CLI command: ``agentwire safety report``."""
    summary = summarize_audit_blocks(days)
    if json_output:
        print(json.dumps(summary, indent=2))
    else:
        print(format_safety_report(summary))
    return 0


def safety_tooldefs_list_cmd() -> int:
    """CLI command: ``agentwire safety tooldefs list``."""
    tooldefs_dir = TOOLDEFS_DIR if TOOLDEFS_DIR.exists() else None
    if tooldefs_dir is None:
        try:
            tooldefs_dir = get_tooldefs_source()
        except FileNotFoundError:
            print("No tooldefs found. Run: agentwire safety install")
            return 1

    files = sorted(tooldefs_dir.glob("*.yaml"))
    if not files:
        print("No tooldefs installed.")
        return 0

    print(f"Available tooldefs ({len(files)}):")
    for f in files:
        try:
            with open(f) as fh:
                data = yaml.safe_load(fh) or {}
            name = data.get("name", f.stem)
            purpose = data.get("purpose", "")
            print(f"  {f.stem:<20} {name} — {purpose}")
        except Exception:
            print(f"  {f.stem}")
    return 0


def safety_tooldefs_show_cmd(tool: str) -> int:
    """CLI command: ``agentwire safety tooldefs show <tool>``."""
    if not yaml:
        print("Error: PyYAML not installed.", file=sys.stderr)
        return 1

    yaml_name = f"{tool}.yaml"
    candidates = []
    if TOOLDEFS_DIR.exists():
        candidates.append(TOOLDEFS_DIR / yaml_name)
    try:
        candidates.append(get_tooldefs_source() / yaml_name)
    except FileNotFoundError:
        pass

    tooldef_file = next((p for p in candidates if p.exists()), None)
    if not tooldef_file:
        print(f"No tooldef found for '{tool}'. Available: agentwire safety tooldefs list")
        return 1

    with open(tooldef_file) as f:
        data = yaml.safe_load(f) or {}

    name = data.get("name", tool)
    purpose = data.get("purpose", "")
    docs = data.get("docs", "")
    commands = data.get("commands", [])

    read_cmds = [c for c in commands if c.get("access") == "read"]
    write_cmds = [c for c in commands if c.get("access") == "write"]
    blocked_cmds = [c for c in commands if c.get("access") == "blocked"]

    print(f"\n{name}")
    print("=" * len(name))
    print(f"Purpose: {purpose}")
    if docs:
        print(f"Docs:    {docs}")
    print()

    green, yellow, red, reset = "\033[32m", "\033[33m", "\033[31m", "\033[0m"

    if read_cmds:
        print(f"{green}READ (always allowed):{reset}")
        for c in read_cmds:
            print(f"  {c['cmd']}")
            print(f"    {c['description']}")
        print()

    if write_cmds:
        print(f"{yellow}WRITE (prompt before executing):{reset}")
        for c in write_cmds:
            print(f"  {c['cmd']}")
            print(f"    {c['description']}")
        print()

    if blocked_cmds:
        print(f"{red}BLOCKED (never execute):{reset}")
        for c in blocked_cmds:
            line = f"  {c['cmd']}"
            if c.get("note"):
                line += f"  [{c['note']}]"
            print(line)
            print(f"    {c['description']}")
        print()

    return 0


# PreToolUse matcher → damage-control hook script. The matcher is the Claude Code
# tool name (regex). MCP tools use the ``mcp__<server>__<tool>`` form; the outbound
# tools that reach real people irreversibly are gated like the Bash shell-out.
DAMAGE_CONTROL_MATCHERS = {
    "Bash": "bash-tool-damage-control.py",
    "Edit": "edit-tool-damage-control.py",
    "Write": "write-tool-damage-control.py",
    # Content-reading tools: PreToolUse fires (and can block) for these, so a
    # zero-access secret read can't slip past the shell/edit/write hooks.
    "Read": "read-tool-damage-control.py",
    "Grep": "read-tool-damage-control.py",
    "Glob": "read-tool-damage-control.py",
    # NotebookEdit writes files without matching Edit or Write (#923).
    "NotebookEdit": "edit-tool-damage-control.py",
    # Every MCP tool, ours and third-party (#923): outbound tools are gated
    # like their Bash shell-outs; every other tool call gets its path-valued
    # arguments screened against zeroAccessPaths and (for write-shaped tools)
    # the protected control plane. The hook exits 0 fast for anything else.
    "mcp__.*": "mcp-tool-damage-control.py",
}


def register_damage_control_in_settings() -> int:
    """Ensure every damage-control PreToolUse matcher is in ``~/.claude/settings.json``.

    Idempotent: only appends matchers/commands that aren't already present.
    Returns the number of entries added.
    """
    settings_file = Path.home() / ".claude" / "settings.json"
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])

    added = 0
    for matcher, hook_file in DAMAGE_CONTROL_MATCHERS.items():
        command = f"~/.agentwire/hooks/damage-control/{hook_file}"
        # Dedup on the (matcher, command) pair, not the command alone: several
        # matchers (Read/Grep/Glob) legitimately share one hook script, so a
        # command-only check would register only the first and silently drop the
        # rest.
        already = any(
            entry.get("matcher") == matcher and h.get("command") == command
            for entry in pre
            for h in entry.get("hooks", [])
        )
        if already:
            continue
        pre.append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command}],
        })
        added += 1

    if added:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(settings, indent=2))
    return added


def _file_drift_state(target: Path, source: Path) -> str:
    """Drift state of an installed damage-control file: missing | stale | ok.

    ``ok`` when the bundled source is absent (nothing to compare) or the
    installed bytes match it; ``missing`` when the source ships but no copy is
    installed; ``stale`` when an installed copy differs from the bundled bytes.

    Equality only. For files that carry a version stamp use
    ``damage_control_hook_drift``, which can also say WHICH WAY the difference
    runs — the distinction #936 is about.
    """
    if not source.exists():
        return "ok"
    if not target.exists():
        return "missing"
    try:
        return "ok" if target.read_bytes() == source.read_bytes() else "stale"
    except OSError:
        return "stale"


# --------------------------------------------------------------------------
# Version ordering for the generated hook scripts (#936)
# --------------------------------------------------------------------------
#
# The heal used to overwrite a hook whenever the bytes DIFFERED, in either
# direction. Combined with `Path(__file__).parent` as the source, that let a
# session on a stale branch reinstall a pre-fix security hook for the whole
# machine — and `shutil.copy2` preserved the source mtime, so the downgrade was
# invisible even to a timestamp check. Provenance (see `safety.provenance`) is
# the guard; this stamp is what lets `doctor` NAME the direction.

_STAMP_VAR = "AGENTWIRE_HOOK_STAMP"
_STAMP_RE = re.compile(rf"^{_STAMP_VAR} = (\{{.*\}})\s*$", re.MULTILINE)


def read_hook_stamp(path: Path) -> Optional[Dict[str, Any]]:
    """Parse a hook script's generated stamp, or None if it carries none.

    Only the five generated hooks are stamped; ``audit_logger.py`` is hand-
    written and has no stamp, so its drift stays unordered by design.
    """
    try:
        m = _STAMP_RE.search(path.read_text(errors="replace"))
    except OSError:
        return None
    if not m:
        return None
    try:
        stamp = json.loads(m.group(1))
    except ValueError:
        return None
    return stamp if isinstance(stamp, dict) else None


def _stamp_order(installed: Path, source: Path) -> str:
    """``older`` | ``newer`` | ``stale`` for two differing stamped hooks.

    ``stale`` means "differs, and nothing here can order them" — either copy
    lacks a stamp, or the two stamps carry the same generation time.
    """
    a, b = read_hook_stamp(installed), read_hook_stamp(source)
    if not a or not b:
        return "stale"
    ta, tb = a.get("generated_at"), b.get("generated_at")
    if not isinstance(ta, str) or not isinstance(tb, str) or ta == tb:
        return "stale"
    return "older" if ta < tb else "newer"


def damage_control_hook_drift() -> Dict[str, str]:
    """Drift state per DC hook script (``bash/edit/write/mcp-tool-...`` + logger).

    ``{filename: ok | missing | older | newer | stale}`` comparing the installed
    copies in ``~/.agentwire/hooks/damage-control/`` against the bundled package
    source. ``newer`` is the #936 signal: the machine is carrying a hook this
    package predates, so installing from here would DOWNGRADE it.
    """
    hooks_source = Path(__file__).parent / "hooks" / "damage-control"
    states: Dict[str, str] = {}
    for fn in DAMAGE_CONTROL_FILES:
        src = hooks_source / fn
        target = HOOKS_DIR / fn
        state = _file_drift_state(target, src)
        states[fn] = _stamp_order(target, src) if state == "stale" else state
    return states


# --------------------------------------------------------------------------
# Three-way sync for rules + tooldefs (#916)
# --------------------------------------------------------------------------
#
# Rules and tooldefs are host-editable, so a blanket overwrite is off the table:
# clobbering a hand-written rule is a worse failure than shipping a stale one.
# But "install missing only" meant they were written once and NEVER updated, so
# every rule fix this repo ships has been inert on every existing machine.
#
# The missing leg of a three-way merge is the common ancestor, and
# `agentwire/safety/rule_baselines.json` ships it: the sha256 of every version
# of each file that has ever shipped. A live file matching one of those is a
# pristine older release and is safe to bring forward; a live file matching
# none of them was edited by hand and is left alone and reported.

_BASELINES_PATH = Path(__file__).parent / "safety" / "rule_baselines.json"


def load_rule_baselines() -> Dict[str, Dict[str, List[str]]]:
    """The shipped-hash manifest: ``{"rules"|"tooldefs": {filename: [sha256]}}``."""
    try:
        data = json.loads(_BASELINES_PATH.read_text())
    except (OSError, ValueError):
        return {"rules": {}, "tooldefs": {}}
    return {
        section: data.get(section) or {}
        for section in ("rules", "tooldefs")
    }


def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _sync_state(target: Path, source: Path, shipped: List[str]) -> str:
    """``ok`` | ``missing`` | ``outdated`` | ``unknown`` for one YAML file.

    ``outdated`` — the installed bytes are a version we shipped, just not the
    current one. Safe to update; nothing local is lost.
    ``unknown`` — the installed bytes match no version we ever shipped. Called
    UNKNOWN and not "customized" on purpose: it is usually a hand edit, but it
    is also what a file older than our recorded history looks like, and we
    cannot tell those apart. So it is reported and left alone rather than
    classified into either bug; ``--force`` replaces it, keeping a backup.
    """
    if not target.exists():
        return "missing"
    digest = _sha256(target)
    if digest is not None and digest == _sha256(source):
        return "ok"
    if digest is not None and digest in (shipped or []):
        return "outdated"
    return "unknown"


def _yaml_sync_states(source_dir: Path, target_dir: Path, section: str) -> Dict[str, str]:
    shipped = load_rule_baselines().get(section, {})
    return {
        src.name: _sync_state(target_dir / src.name, src, shipped.get(src.name, []))
        for src in sorted(source_dir.glob("*.yaml"))
    }


def rules_drift() -> Dict[str, str]:
    """Sync state per bundled rule file vs ``~/.agentwire/damage-control/``.

    ``{filename: ok | missing | outdated | unknown}``. Only the bundled rule
    set is inspected (a user-added rule with no bundled counterpart is not
    drift).
    """
    try:
        source_dir = get_damage_control_source()
    except FileNotFoundError:
        return {}
    return _yaml_sync_states(source_dir, RULES_DIR, "rules")


def tooldefs_drift() -> Dict[str, str]:
    """Sync state per bundled tooldef file vs ``~/.agentwire/tooldefs/``.

    Reported separately from rules because a drifted tooldef fails a DIFFERENT
    way: it loses the pinned ``id:`` fields, which is what silently killed the
    whole of ``DEFAULT_UNATTENDED_ALLOW`` on the machine that filed #916.
    """
    try:
        source_dir = get_tooldefs_source()
    except FileNotFoundError:
        return {}
    return _yaml_sync_states(source_dir, TOOLDEFS_DIR, "tooldefs")


def _rule_entries(path: Path) -> Dict[str, List[str]]:
    """Index one rule YAML by protection: ``{section: [key, ...]}``.

    Bash rules are keyed by their regex (the thing that decides), path rules by
    the path. Keys, not counts: doctor has to be able to NAME what is missing.
    """
    out: Dict[str, List[str]] = {}
    if not yaml:
        return out
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, ValueError, Exception):  # noqa: B014 - yaml raises its own
        return out
    if not isinstance(data, dict):
        return out
    for key in ("bashToolPatterns", "zeroAccessPaths", "readOnlyPaths", "noDeletePaths"):
        items = data.get(key) or []
        keys = []
        for it in items:
            if isinstance(it, dict):
                k = it.get("pattern") or it.get("path")
                if k:
                    keys.append(str(k))
            elif isinstance(it, str):
                keys.append(it)
        if keys:
            out[key] = keys
    return out


def rule_file_delta(live: Path, bundled: Path) -> Dict[str, List[str]]:
    """``{"missing": [...], "extra": [...]}`` — protections, not bytes.

    ``missing`` is bundled protection absent from the live file: drift that
    REMOVES a guard. ``extra`` is a local addition. Doctor grades those
    differently because they are not the same event, and reporting both as
    "differs from bundled" is what let an entire deploy tier sit disabled while
    the line above it read ``[ok]`` (#916).
    """
    live_idx, bundled_idx = _rule_entries(live), _rule_entries(bundled)
    missing: List[str] = []
    extra: List[str] = []
    for section in set(live_idx) | set(bundled_idx):
        live_keys = set(live_idx.get(section, []))
        bundled_keys = set(bundled_idx.get(section, []))
        missing.extend(sorted(bundled_keys - live_keys))
        extra.extend(sorted(live_keys - bundled_keys))
    return {"missing": missing, "extra": extra}


def missing_damage_control_matchers() -> List[str]:
    """Damage-control matchers not registered in ``~/.claude/settings.json``.

    Checks the (matcher, command) PAIR, mirroring
    ``register_damage_control_in_settings``'s dedup. Several matchers
    (Read/Grep/Glob) legitimately share one hook script, so a command-only
    check would treat all three as present the moment *any* one is registered —
    silently hiding a real gap in a security hook (e.g. Read guarded but
    Grep/Glob not).
    """
    settings_file = Path.home() / ".claude" / "settings.json"
    try:
        settings = json.loads(settings_file.read_text())
    except (OSError, json.JSONDecodeError):
        settings = {}
    pre = settings.get("hooks", {}).get("PreToolUse", []) if isinstance(settings, dict) else []
    registered_pairs = {
        (entry.get("matcher"), h.get("command"))
        for entry in pre if isinstance(entry, dict)
        for h in (entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else [])
        if isinstance(h, dict)
    }
    missing = []
    for matcher, hook_file in DAMAGE_CONTROL_MATCHERS.items():
        command = f"~/.agentwire/hooks/damage-control/{hook_file}"
        if (matcher, command) not in registered_pairs:
            missing.append(matcher)
    return missing


def _write_installed(src: Path, target: Path, executable: bool = False) -> None:
    """Copy bundled bytes to an installed path with a FRESH mtime.

    Deliberately not ``shutil.copy2``: preserving the source mtime is what made
    #936's downgrade invisible — the reinstalled pre-fix hook carried an mtime
    EARLIER than the deployment it had just replaced, so "is my install newer
    than the package?" could not see it. An install is an event; it should look
    like one.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(src.read_bytes())
    if executable:
        target.chmod(0o755)


def _backup_unknown(target: Path) -> Path:
    """Move a hand-edited file aside before ``--force`` overwrites it."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.local-{stamp}.bak")
    shutil.move(str(target), str(backup))
    return backup


def _sync_yaml_dir(
    source_dir: Path,
    target_dir: Path,
    section: str,
    force: bool,
    log,
    label: str,
) -> Dict[str, List[str]]:
    """Three-way sync one bundled YAML dir into its installed counterpart."""
    result: Dict[str, List[str]] = {"installed": [], "updated": [], "unknown": []}
    shipped = load_rule_baselines().get(section, {})
    for src in sorted(source_dir.glob("*.yaml")):
        target = target_dir / src.name
        state = _sync_state(target, src, shipped.get(src.name, []))
        if state == "ok":
            continue
        if state == "missing":
            _write_installed(src, target)
            result["installed"].append(src.name)
            log(f"✓ Installed {label} {src.name}")
        elif state == "outdated":
            _write_installed(src, target)
            result["updated"].append(src.name)
            log(f"✓ Updated {label} {src.name} (was a previously shipped version)")
        elif force:
            backup = _backup_unknown(target)
            _write_installed(src, target)
            result["updated"].append(src.name)
            log(f"✓ Replaced unrecognized {label} {src.name} (backup: {backup.name})")
        else:
            result["unknown"].append(src.name)
            log(
                f"⚠️  Left {label} {src.name} alone — its content matches NO version "
                f"we ever shipped, so it is either hand-edited or older than our "
                f"recorded history, and nothing here can tell those apart. "
                f"`--force` replaces it (a .local-<ts>.bak is kept)."
            )
    return result


def heal_damage_control(
    quiet: bool = False,
    force: bool = False,
    allow_foreign: bool = False,
) -> Dict[str, Any]:
    """Drift-aware sync of DC hook scripts, rules, tooldefs, and matchers.

    Non-interactive. Three properties are load-bearing and were each a defect:

    **Provenance (#936).** Machine-global files are written only from the
    canonically installed tool. A checkout that is not the installed tool
    refuses outright rather than pushing its own — possibly pre-security-fix —
    copies over everything on the box.

    **Ordering, not equality (#936).** A stamped hook that is NEWER than this
    package is never overwritten without ``force``; the old code copied on any
    difference, in either direction.

    **Three-way, not install-missing-only (#916).** A rule/tooldef whose bytes
    match a previously shipped version is brought forward; one that matches no
    shipped version is treated as hand-edited and left alone. The old code never
    updated either, so every shipped rule fix was inert on existing installs.

    Two overrides, deliberately separate because they answer different
    questions: ``allow_foreign`` says *this checkout may write machine-global
    files*; ``force`` says *overwrite content I would otherwise hold back*
    (a newer installed hook, a hand-edited rule — backed up first).

    Returns a summary dict of what changed.
    """
    log = (lambda *a: None) if quiet else print

    summary: Dict[str, Any] = {
        "hooks_installed": [], "hooks_updated": [], "hooks_downgrade_refused": [],
        "rules_installed": [], "rules_updated": [], "rules_unknown": [],
        "tooldefs_installed": [], "tooldefs_updated": [], "tooldefs_unknown": [],
        "matchers_added": 0, "policy_scaffolded": False,
        "refused": False, "provenance": "",
    }

    from agentwire.safety import provenance as prov

    state, canonical, running = prov.install_provenance()
    summary["provenance"] = state
    if state in prov.REFUSING_STATES and not allow_foreign:
        summary["refused"] = True
        for line in prov.refusal_lines(canonical, running, "damage-control hooks/rules"):
            log(line)
        return summary

    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    TOOLDEFS_DIR.mkdir(parents=True, exist_ok=True)

    # Host-owned policy file: create with `enabled: true` (fail-secure) if absent.
    # Never overwrite an existing one — it carries the owner's kill-switch state.
    if scaffold_damagecontrol_file():
        summary["policy_scaffolded"] = True
        log(f"✓ Scaffolded {DAMAGECONTROL_FILE} (enabled: true)")

    hooks_source = Path(__file__).parent / "hooks" / "damage-control"
    for fn in DAMAGE_CONTROL_FILES:
        src = hooks_source / fn
        if not src.exists():
            log(f"⚠️  Missing {fn} in package")
            continue
        target = HOOKS_DIR / fn
        state = _file_drift_state(target, src)
        if state == "ok":
            continue
        if state == "stale":
            state = _stamp_order(target, src)
        if state == "newer" and not force:
            summary["hooks_downgrade_refused"].append(fn)
            log(
                f"⚠️  REFUSED to downgrade {fn}: the installed copy is NEWER than this "
                f"package. Pull and rebuild, or pass --force to overwrite it anyway."
            )
            continue
        _write_installed(src, target, executable=fn.endswith(".py"))
        if state == "missing":
            summary["hooks_installed"].append(fn)
            log(f"✓ Installed hook {fn}")
        else:
            summary["hooks_updated"].append(fn)
            log(f"✓ Updated {state} hook {fn}")

    try:
        rules = _sync_yaml_dir(
            get_damage_control_source(), RULES_DIR, "rules", force, log, "rule"
        )
        summary["rules_installed"] = rules["installed"]
        summary["rules_updated"] = rules["updated"]
        summary["rules_unknown"] = rules["unknown"]
    except FileNotFoundError as e:
        log(f"⚠️  {e}")

    try:
        tooldefs = _sync_yaml_dir(
            get_tooldefs_source(), TOOLDEFS_DIR, "tooldefs", force, log, "tooldef"
        )
        summary["tooldefs_installed"] = tooldefs["installed"]
        summary["tooldefs_updated"] = tooldefs["updated"]
        summary["tooldefs_unknown"] = tooldefs["unknown"]
    except FileNotFoundError as e:
        log(f"⚠️  {e}")

    if summary["tooldefs_updated"] or summary["tooldefs_installed"]:
        for line in unattended_grant_notice():
            log(line)

    added = register_damage_control_in_settings()
    summary["matchers_added"] = added
    if added:
        log(f"✓ Registered {added} damage-control matcher{'s' if added != 1 else ''}")

    return summary


def unattended_grant_notice() -> List[str]:
    """The loud notice that a tooldef refresh is a PERMISSIONS CHANGE (#916).

    Repairing tooldef drift restores the pinned ``id:`` fields, which is what
    ``DEFAULT_UNATTENDED_ALLOW`` names. On a machine whose tooldefs had drifted,
    five of those six grants matched nothing at all, so the grants had never
    been in force. Bringing the files forward therefore does not "restore" the
    default — in practice it GRANTS it. It reads as a chore and lands as a
    permissions change, so it says so out loud rather than in a changelog.
    """
    from agentwire.safety._core import DEFAULT_UNATTENDED_ALLOW
    return [
        "",
        "!! TOOLDEFS CHANGED — this is a PERMISSIONS CHANGE, not a maintenance no-op.",
        "   Tooldefs carry the stable `id:` fields that DEFAULT_UNATTENDED_ALLOW names.",
        "   With them in place, UNATTENDED (scheduler) runs may now resolve these to",
        "   allow, in ANY repo:",
        f"     {', '.join(DEFAULT_UNATTENDED_ALLOW)}",
        "   That is what the code has always specified. If the ids were missing before,",
        "   those grants were inert and this switches them ON for the first time.",
        "   To narrow them: `unattended_allow` in ~/.agentwire/damagecontrol.yml",
        "   (path-scoped entries supported, #914).",
        "",
    ]


def safety_install_cmd(
    assume_yes: bool = False,
    force: bool = False,
    allow_foreign: bool = False,
) -> int:
    """CLI command: ``agentwire safety install``.

    With ``--yes``/``assume_yes`` it runs unattended and drift-aware: installs
    missing hook scripts/rules/tooldefs, updates stale *owned* hook scripts,
    brings previously-shipped rule/tooldef versions forward, and registers any
    absent matchers — without prompting, without downgrading a newer installed
    hook, and without clobbering a hand-edited rule. Without it, the same heal
    runs behind the two interactive confirmations.

    ``--allow-foreign-source`` permits a non-canonical checkout to write;
    ``--force`` overwrites content otherwise held back. Returns 1 when anything
    was refused or held back, so a script can never read a refusal as success —
    which is the whole of #936's operator picture.
    """
    print("AgentWire Safety Installation")
    print("=" * 50)
    print()

    if not assume_yes:
        if HOOKS_DIR.exists() and RULES_DIR.exists() and any(RULES_DIR.glob("*.yaml")):
            print("⚠️  Safety hooks already installed")
            print(f"   Location: {HOOKS_DIR}")
            if input("Re-sync (drift-aware heal)? [y/N] ").strip().lower() != "y":
                print("Installation cancelled.")
                return 0

        print("This will install/heal damage control security hooks at:")
        print(f"  {HOOKS_DIR}")
        print()
        print("The hooks will:")
        print("  • Block dangerous commands (rm -rf /, etc.)")
        print("  • Protect sensitive files (.env, SSH keys, etc.)")
        print("  • Log all security decisions")
        print()

        if input("Proceed with installation? [y/N] ").strip().lower() != "y":
            print("Installation cancelled.")
            return 0

    try:
        get_damage_control_source()
    except FileNotFoundError as e:
        print(f"\n⚠️  {e}")
        print("   The damage-control hooks are missing from the package.")
        return 1

    print()
    summary = heal_damage_control(force=force, allow_foreign=allow_foreign)

    if summary["refused"]:
        return 1

    touched = any(
        summary[k] for k in (
            "hooks_installed", "hooks_updated",
            "rules_installed", "rules_updated",
            "tooldefs_installed", "tooldefs_updated",
            "matchers_added", "policy_scaffolded",
        )
    )
    held_back = summary["hooks_downgrade_refused"] or summary["rules_unknown"] or summary["tooldefs_unknown"]
    print()
    if touched:
        print("✓ Damage control synced.")
    elif held_back:
        print("⚠️  Damage control NOT fully synced — see the held-back files above.")
    else:
        print("✓ Damage control already in sync — nothing to do.")
    if held_back:
        return 1
    print()
    print("Next steps:")
    print("  1. Test with: agentwire safety check 'rm -rf /'")
    print("  2. View status: agentwire safety status")
    print("  3. Add tool rules: ~/.agentwire/damage-control/<tool>.yaml")
    print("  4. View tool commands: agentwire safety tooldefs list")
    return 0
