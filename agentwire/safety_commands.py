"""Safety CLI commands for AgentWire damage control integration.

This module is the CLI front end and the installer. All decision logic — pattern matching,
allowlist evaluation, the decision ladder — lives in the ``damage_control`` package, which
the hooks run as ``python -I -m damage_control.hook`` (#1209 M4).
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentwire import safety_ext

try:
    import yaml
except ImportError:
    yaml = None

# Re-exported from damage_control.engine so callers and tests can import the whole
# damage-control surface from agentwire.safety_commands.
from damage_control.engine import (  # noqa: F401
    ALL_OPERATIONS,
    HOOK_TIMEOUT_SECONDS,
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
    load_effective_config,
    match_path,
    matches_path_in_command,
)

# Default config directory
CONFIG_DIR = Path.home() / ".agentwire"
LOGS_DIR = CONFIG_DIR / "logs" / "damage-control"
RULES_DIR = CONFIG_DIR / "damage-control"
TOOLDEFS_DIR = CONFIG_DIR / "tooldefs"
# Host-owned, agent-unwritable policy file: kill switch + rule knobs (#466).
DAMAGECONTROL_FILE = CONFIG_DIR / "damagecontrol.yml"


def load_patterns() -> Dict[str, Any]:
    """The merged rule set this machine enforces: bundled rules, then the overlay, minus what the policy switched off."""
    return load_effective_config()


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
    package = safety_ext.status()

    from damage_control.engine import _merge_file_sources, load_safety_config, resolve_sources

    safety_cfg = load_safety_config()
    sources = resolve_sources(safety_cfg)
    rule_paths, _ = _merge_file_sources(sources.rules_dir, sources.overlay_rules, sources.skip_rules)
    tooldef_paths, _ = _merge_file_sources(sources.tooldefs_dir, sources.overlay_tooldefs, sources.skip_tooldefs)
    rule_files = [p.stem for p in rule_paths]
    tooldefs_count = len(tooldef_paths)

    status: Dict[str, Any] = {
        # The package's own verdict (`status --json` under AgentWire's profile): registered,
        # recorded, interpreter alive, no drift. Scripts on disk prove nothing; Claude Code only
        # calls what Claude's settings register (#1085).
        "package": package,
        "hooks_installed": not any(f["code"] == "not-installed" for f in package["findings"]),
        "enabled": safety_cfg.get("enabled", True),
        "disabled_rules": list(safety_cfg.get("disabled_rules", []) or []),
        "policy_file": str(DAMAGECONTROL_FILE),
        "policy_file_exists": DAMAGECONTROL_FILE.exists(),
        "rules_dir": str(RULES_DIR),
        "patterns_exist": bool(patterns.get("bashToolPatterns")),
        "rule_files": rule_files,
        "logs_dir": str(LOGS_DIR),
        "logs_exist": LOGS_DIR.exists(),
        "tooldefs_dir": str(TOOLDEFS_DIR),
        "tooldefs_count": tooldefs_count,
        # Two live rules sharing one id makes `disabled_rules` /
        # `unattended_allow` ambiguous, and is the only visible symptom of a
        # half-applied rule sync (#916).
        "duplicate_rule_ids": list(patterns.get("_duplicate_rule_ids") or []),
        "parse_failures": list(patterns.get("_parse_failures") or []),
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

    package = status["package"]
    lines.append(f"✓ damage-control-dev {package['version']}, profile '{package['profile']}'")
    for finding in package["findings"]:
        mark = {"ok": "✓", "info": "·", "bad": "⚠️ "}[finding["level"]]
        lines.append(f"  {mark} {finding['message']}")
    if not package["ok"]:
        lines.append("   Damage control may NOT be running. Fix: agentwire safety install --yes")
    lines.append(f"✓ Rules: bundled with the package; overlay directory {status['rules_dir']}")
    if status.get("rule_files"):
        lines.append(f"  Loaded: {', '.join(status['rule_files'])}")
    lines.append("")

    lines.append("Pattern Counts:")
    for name, count in status["pattern_counts"].items():
        lines.append(f"  • {name.replace('_', ' ').title()}: {count}")
    lines.append("")

    lines.append(f"Audit Logs: {status['logs_dir']}")
    lines.append(f"  Exists: {status['logs_exist']}")
    lines.append("")

    if "tooldefs_dir" in status:
        lines.append(f"Tooldefs: bundled; overlay directory {status['tooldefs_dir']}")
        lines.append(f"  Loaded: {status['tooldefs_count']} tool definitions")
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
    outcomes = summary.get("outcomes")
    if outcomes and outcomes["labelled"]:
        lines += [
            "",
            f"Outcomes ({outcomes['labelled']} of {outcomes['decisions']} decisions joined "
            "to what happened next):",
        ]
        for label, count in sorted(outcomes["by_label"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:6d}  {label}")
        if outcomes["friction"]:
            lines += ["", "Friction by rule (approved-most first; rate = approved / answered prompts):"]
            lines.append("  approved declined blocked  rate  rule")
            for r in outcomes["friction"][:15]:
                rate = "  -  " if r["approve_rate"] is None else f"{r['approve_rate']:5.2f}"
                lines.append(f"  {r['approved']:8d} {r['declined']:8d} {r['blocked']:7d} {rate}  {r['rule']}")
        if outcomes["by_day"]:
            lines += ["", "Per day (stopped = approved + declined + blocked):"]
            for day, counts in outcomes["by_day"].items():
                stopped = sum(counts.get(k, 0) for k in ("approved", "declined-or-abandoned", "rule-blocked"))
                total = sum(counts.values())
                lines.append(f"  {day}  {stopped:4d} stopped of {total}")
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
    from agentwire.safety.audit import summarize_outcomes

    summary["outcomes"] = summarize_outcomes(days, LOGS_DIR)
    if json_output:
        print(json.dumps(summary, indent=2))
    else:
        print(format_safety_report(summary))
    return 0


def safety_suggest_delete_recursive_cmd(days: int = 14, min_count: int = 2, json_output: bool = False) -> int:
    """CLI command: ``agentwire safety suggest-delete-recursive`` (read-only; prints the policy to paste)."""
    from agentwire.safety.suggest import format_suggestions, suggest_delete_recursive

    result = suggest_delete_recursive(days, min_count, LOGS_DIR)
    print(json.dumps(result, indent=2) if json_output else format_suggestions(result))
    return 0


def _tooldef_files() -> Dict[str, Path]:
    """``{stem: YAML path}`` of the tooldefs a human can read: bundled sources, the overlay's winning."""
    from damage_control.engine import bundled_tooldefs_dir

    found: Dict[str, Path] = {p.stem: p for p in sorted(bundled_tooldefs_dir().glob("*.yaml"))}
    if TOOLDEFS_DIR.is_dir():
        found.update({p.stem: p for p in sorted(TOOLDEFS_DIR.glob("*.yaml"))})
    return dict(sorted(found.items()))


def safety_tooldefs_list_cmd() -> int:
    """CLI command: ``agentwire safety tooldefs list``."""
    files = _tooldef_files()
    if not files:
        print("No tooldefs found.")
        return 0

    print(f"Available tooldefs ({len(files)}):")
    for stem, f in files.items():
        try:
            with open(f) as fh:
                data = yaml.safe_load(fh) or {}
            name = data.get("name", stem)
            purpose = data.get("purpose", "")
            print(f"  {stem:<20} {name} — {purpose}")
        except Exception:
            print(f"  {stem}")
    return 0


def safety_tooldefs_show_cmd(tool: str) -> int:
    """CLI command: ``agentwire safety tooldefs show <tool>``."""
    if not yaml:
        print("Error: PyYAML not installed.", file=sys.stderr)
        return 1

    tooldef_file = _tooldef_files().get(tool)
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


def safety_install_cmd(
    assume_yes: bool = False,
    force: bool = False,
    allow_foreign: bool = False,
) -> int:
    """CLI command: ``agentwire safety install``: the package's ``init`` under AgentWire's profile.

    Without ``--yes`` it asks once first. Returns non-zero on any refusal, so a script can never
    read a refusal as success (the whole of #936's operator picture).
    """
    if not assume_yes:
        print("This registers the damage-control hooks (damage-control-dev) in Claude Code's user settings:")
        print(f"  {sys.executable} -I -m damage_control.hook <bash|edit|write|read|mcp>")
        if input("Proceed with installation? [y/N] ").strip().lower() != "y":
            print("Installation cancelled.")
            return 0
    return safety_ext.install(force=force, allow_foreign=allow_foreign).code
