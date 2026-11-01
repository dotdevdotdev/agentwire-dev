"""Safety CLI commands for AgentWire damage control integration.

This module is the CLI front end and the installer. All decision logic — pattern matching,
allowlist evaluation, the decision ladder — lives in the ``damage_control`` package, which
the hooks run as ``python -I -m damage_control.hook`` (#1209 M4).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentwire.claude_settings import SettingsError, edit_claude_settings
from agentwire.utils.paths import claude_dir

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
LEGACY_HOOKS_DIR = CONFIG_DIR / "hooks" / "damage-control"  # pre-package scripts, removed by heal
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

# Rule groups and vendor packs (#1141). Written by `agentwire safety packs|groups`
# (human-only). `packs` = enabled vendor packs (aws, databases, ...); the key is
# added on first heal. `disabled_groups` = opted-out default groups; the kernel
# (control-plane, core) can never be disabled.
# packs: [aws]
# disabled_groups: [outbound]
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

    from damage_control.engine import _merge_file_sources, load_safety_config, resolve_sources

    safety_cfg = load_safety_config()
    sources = resolve_sources(safety_cfg)
    rule_paths, _ = _merge_file_sources(sources.rules_dir, sources.overlay_rules, sources.skip_rules)
    tooldef_paths, _ = _merge_file_sources(sources.tooldefs_dir, sources.overlay_tooldefs, sources.skip_tooldefs)
    rule_files = [p.stem for p in rule_paths]
    tooldefs_count = len(tooldef_paths)

    status: Dict[str, Any] = {
        # The package hooks have no files to find; "installed" means registered AND recorded.
        "hooks_installed": INSTALL_RECORD.exists(),
        # Scripts on disk prove nothing: Claude Code only calls the ones
        # registered in settings.json (#1085).
        "missing_matchers": missing_damage_control_matchers(),
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

    lines.append(f"✓ Hooks recorded: {INSTALL_RECORD}")
    if status["missing_matchers"]:
        lines.append(
            "⚠️  NOT WIRED UP — PreToolUse matchers missing from ~/.claude/settings.json: "
            f"{', '.join(status['missing_matchers'])}"
        )
        lines.append("   Damage control is NOT running. Fix: agentwire safety install --yes")
    else:
        lines.append("✓ PreToolUse matchers registered in ~/.claude/settings.json")
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


# The sha256 of every version of each rule/tooldef that ever shipped
# (`agentwire/safety/rule_baselines.json`). A copy in the overlay dir matching one is a
# leftover from the days heal copied the bundle onto the machine; one matching none is a
# human's own edit (#916, #1209 M4).
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


def _carry_forward_carved_packs(state: Dict[str, Any]) -> List[str]:
    """Enable ``tmux`` on a machine whose installed ``agentwire.yaml`` still carries it (#1209 M2).

    ``kill-server`` / ``kill-session -a`` used to ship inside ``agentwire.yaml``
    (always on). They now live in the opt-in ``tmux`` pack, so a machine whose
    ``packs:`` list was already decided would silently lose both on the next heal.
    The marker is the INSTALLED ``agentwire.yaml`` being a pristine older release:
    that file held the rules, so the decision was "on". It is one-shot — once heal
    retires the copy the marker is gone, and a later ``packs disable tmux``
    sticks. An edited file matches no shipped hash and is left to the human.
    """
    from damage_control import packs
    from damage_control.groups import default_packs

    from agentwire.safety import host_profile

    wanted = [p for p in default_packs() if p not in (state[packs.PACKS_KEY] or [])]
    installed = RULES_DIR / "agentwire.yaml"
    if not wanted or not installed.exists():
        return []
    digest = _sha256(installed)
    current = _sha256(host_profile.RULES_DIR / "agentwire.yaml")
    shipped = load_rule_baselines().get("rules", {}).get("agentwire.yaml", [])
    if digest is None or digest == current or digest not in shipped:
        return []
    state[packs.PACKS_KEY] = sorted([*(state[packs.PACKS_KEY] or []), *wanted])
    packs.write_state(DAMAGECONTROL_FILE, packs=state[packs.PACKS_KEY])
    return wanted


def migrate_pack_state() -> Dict[str, Any]:
    """Load pack/group state, first recording a pre-packs install's packs (#1141).

    While ``packs:`` is absent from the policy file, every pack with any file
    already installed is written as enabled, so upgrading changes no decision.
    Returns the state dict plus ``migrated`` (the packs just recorded).
    """
    from damage_control import packs

    state = packs.read_state(DAMAGECONTROL_FILE)
    migrated: List[str] = []
    if state["broken"]:
        pass  # unparseable: protective read, never written over (doctor reports it)
    elif state[packs.PACKS_KEY] is None:
        installed = {
            "rules": {p.name for p in RULES_DIR.glob("*.yaml")} if RULES_DIR.is_dir() else set(),
            "tooldefs": {p.name for p in TOOLDEFS_DIR.glob("*.yaml")} if TOOLDEFS_DIR.is_dir() else set(),
        }
        migrated = packs.migrate_packs(state, installed)
        packs.write_state(DAMAGECONTROL_FILE, packs=migrated)
        state[packs.PACKS_KEY] = migrated
    else:
        migrated = _carry_forward_carved_packs(state)
    state["migrated"] = migrated
    return state


# PreToolUse matcher → the hook kind that judges it (``python -I -m damage_control.hook
# <kind>``). The matcher is the Claude Code tool name (regex). MCP tools use the
# ``mcp__<server>__<tool>`` form; the outbound tools that reach real people irreversibly are
# gated like the Bash shell-out.
DAMAGE_CONTROL_MATCHERS = {
    "Bash": "bash",
    # The PowerShell tool (CLAUDE_CODE_USE_POWERSHELL_TOOL; the only shell on a
    # Windows box without Git Bash) is a separate tool name whose
    # ``tool_input.command`` has Bash's shape: same hook, own entry (#1218).
    "PowerShell": "bash",
    "Edit": "edit",
    "Write": "write",
    # Content-reading tools: PreToolUse fires (and can block) for these, so a
    # zero-access secret read can't slip past the shell/edit/write hooks.
    "Read": "read",
    "Grep": "read",
    "Glob": "read",
    # NotebookEdit writes files without matching Edit or Write (#923).
    "NotebookEdit": "edit",
    # Every MCP tool, ours and third-party (#923): outbound tools are gated
    # like their Bash shell-outs; every other tool call gets its path-valued
    # arguments screened against zeroAccessPaths and (for write-shaped tools)
    # the protected control plane. The hook exits 0 fast for anything else.
    "mcp__.*": "mcp",
}

# The module a registered hook runs. Its presence in a hook's ``args`` is what marks the
# entry as ours.
HOOK_MODULE = "damage_control.hook"
# The pre-package scripts (#1209 M4), registered by path. Registrations of them are
# replaced, and the files removed, once the package hooks are registered and proven.
LEGACY_HOOK_SUFFIX = "-tool-damage-control.py"
LEGACY_HOOK_FILES = (
    "bash-tool-damage-control.py",
    "edit-tool-damage-control.py",
    "write-tool-damage-control.py",
    "read-tool-damage-control.py",
    "mcp-tool-damage-control.py",
    "audit_logger.py",
)
# What the installer recorded about the registration it wrote: the interpreter every hook
# runs under, which ``doctor`` re-verifies.
INSTALL_RECORD = CONFIG_DIR / "damagecontrol-install.json"
BREAKGLASS_DIR = CONFIG_DIR / "bin"


def _hook_args(kind: str) -> List[str]:
    """The arguments after the interpreter: ``-I -m damage_control.hook <kind> --profile <manifest>``."""
    from agentwire.safety import host_profile

    return ["-I", "-m", HOOK_MODULE, kind, "--profile", str(host_profile.manifest_path())]


def _is_ours(hook: Any) -> bool:
    """A hook dict this installer registered (the package form)."""
    args = hook.get("args") if isinstance(hook, dict) else None
    return isinstance(args, list) and HOOK_MODULE in args


def _is_legacy(hook: Any) -> bool:
    """A hook dict that registers one of the pre-package scripts."""
    return isinstance(hook, dict) and str(hook.get("command", "")).endswith(LEGACY_HOOK_SUFFIX)


def _owned_hook_entries(pre: Any, matcher: str, kind: str) -> List[Dict[str, Any]]:
    """Our hook dicts for ``matcher`` in a PreToolUse list; malformed entries are skipped."""
    owned: List[Dict[str, Any]] = []
    if not isinstance(pre, list):
        return owned
    for entry in pre:
        if not isinstance(entry, dict) or entry.get("matcher") != matcher:
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            continue
        owned.extend(h for h in inner if _is_ours(h) and kind in h["args"])
    return owned


def _pre_tool_use() -> Any:
    """The PreToolUse list in ``~/.claude/settings.json``, or None when absent or unreadable."""
    try:
        settings = json.loads((claude_dir() / "settings.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    return hooks.get("PreToolUse") if isinstance(hooks, dict) else None


# Claude Code fails OPEN on a hook crash, timeout, unparsable output, missing
# interpreter, or any exit other than 0/2. ``"onFailure": "block"`` (Claude Code
# >= 2.1.295) turns every one of those into a block (#1219).
ONFAILURE_MIN_CLAUDE_VERSION = (2, 1, 295)


def claude_code_version() -> Optional[tuple]:
    """The installed Claude Code version as an int tuple, or None when unknown."""
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(g) for g in m.groups()) if m else None


def onfailure_supported(version: Optional[tuple]) -> Optional[bool]:
    """True/False once the version is known; None when it could not be read."""
    return None if version is None else version >= ONFAILURE_MIN_CLAUDE_VERSION


def _version_text(version: Optional[tuple]) -> str:
    return ".".join(map(str, version)) if version else "unknown"


def damage_control_selftest(interpreter: Optional[str] = None) -> List[str]:
    """Run each hook kind the way Claude Code does; return what failed.

    Executes ``<interpreter> -I -m damage_control.hook <kind> --profile <manifest>`` exactly
    as registered, so a missing interpreter, an unimportable package, an unreadable profile
    or an unloadable rule set all surface here. Every hook must start and exit 0 on a payload
    for a tool it does not gate; the Bash hook must also BLOCK (exit 2) a destructive
    command, which proves the rules actually loaded.
    """
    interpreter = interpreter or sys.executable
    probes = [(kind, {"tool_name": "AgentwireSelftest", "tool_input": {}}, 0)
              for kind in dict.fromkeys(DAMAGE_CONTROL_MATCHERS.values())]
    probes.extend(("bash", {"tool_name": tool, "tool_input": {"command": "rm -rf /agentwire-selftest"}}, 2)
                  for tool in ("Bash", "PowerShell"))
    failures = []
    for kind, payload, want in probes:
        try:
            got = subprocess.run([interpreter, *_hook_args(kind)], input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=HOOK_TIMEOUT_SECONDS).returncode
        except (OSError, subprocess.SubprocessError) as e:
            failures.append(f"{kind}: {type(e).__name__}: {e}")
            continue
        if got != want:
            failures.append(f"{kind}: exit {got}, expected {want}")
    return failures


def register_damage_control_in_settings(interpreter: Optional[str] = None, fail_closed: bool = True) -> int:
    """Ensure every damage-control PreToolUse matcher is registered in exec form in ``~/.claude/settings.json``.

    Idempotent: appends missing matchers and rewrites our entries whose interpreter, arguments,
    ``timeout`` or (with ``fail_closed``) ``onFailure`` drifted. Registrations of the
    pre-package scripts are REMOVED in the same atomic write, so there is never a moment
    with both or neither. Returns the number of entries added, updated or removed.
    ``fail_closed=False`` leaves ``onFailure`` alone -- the caller's selftest failed, and
    blocking on top of a broken install would brick the session. Raises ``SettingsError``
    (file untouched) when the settings file is unparseable (#1220).
    """
    interpreter = interpreter or sys.executable
    changed = 0

    def mutate(settings: Dict[str, Any]) -> bool:
        nonlocal changed
        changed = 0
        hooks = settings.setdefault("hooks", {})
        pre = hooks.setdefault("PreToolUse", []) if isinstance(hooks, dict) else None
        if not isinstance(pre, list):
            # Malformed hooks.PreToolUse: rewriting it would destroy what the user has.
            print("settings.json has a malformed hooks.PreToolUse — damage-control "
                  "hooks not registered; fix the file and re-run", file=sys.stderr)
            return False

        for entry in list(pre):
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(inner, list) or not any(_is_legacy(h) for h in inner):
                continue
            kept = [h for h in inner if not _is_legacy(h)]
            changed += len(inner) - len(kept)
            if kept:
                entry["hooks"] = kept
            else:
                pre.remove(entry)

        for matcher, kind in DAMAGE_CONTROL_MATCHERS.items():
            # Dedup on the (matcher, kind) pair: several matchers (Read/Grep/Glob) legitimately
            # share one hook kind, so a kind-only check would register only the first.
            wanted = {"type": "command", "command": interpreter, "args": _hook_args(kind),
                      # An explicit timeout: a hook that outlives it is killed and the tool call
                      # RUNS (fail-open), so the scan's own deadline (SCAN_DEADLINE_SECONDS) must
                      # sit well inside this value (#1108).
                      "timeout": HOOK_TIMEOUT_SECONDS}
            owned = _owned_hook_entries(pre, matcher, kind)
            if owned:
                for h in owned:
                    for key, value in wanted.items():
                        if h.get(key) != value:
                            h[key] = value
                            changed += 1
                    if fail_closed and h.get("onFailure") != "block":
                        h["onFailure"] = "block"
                        changed += 1
                continue
            if fail_closed:
                wanted["onFailure"] = "block"
            pre.append({"matcher": matcher, "hooks": [wanted]})
            changed += 1
        return bool(changed)

    edit_claude_settings(mutate)
    return changed


def _read_install_record() -> Dict[str, Any]:
    try:
        data = json.loads(INSTALL_RECORD.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_install_record(interpreter: str) -> None:
    """Record the interpreter the hooks were registered with (``doctor`` re-verifies it)."""
    import damage_control

    record = {
        "interpreter": interpreter,
        "python": ".".join(map(str, sys.version_info[:3])),
        "damage_control_version": damage_control.__version__,
        "package_path": str(Path(damage_control.__file__).resolve().parent),
        "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    INSTALL_RECORD.parent.mkdir(parents=True, exist_ok=True)
    tmp = INSTALL_RECORD.with_name(INSTALL_RECORD.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    tmp.replace(INSTALL_RECORD)


def interpreter_state() -> Dict[str, Any]:
    """``{"interpreter", "state"}``: ``ok`` | ``dead`` | ``unrecorded``.

    ``dead`` means the recorded interpreter is gone or cannot import the package: with
    ``onFailure: block`` every tool call is refused until the install is repaired.
    """
    interpreter = _read_install_record().get("interpreter")
    if not interpreter:
        return {"interpreter": None, "state": "unrecorded"}
    try:
        ok = subprocess.run([interpreter, "-I", "-c", "import damage_control"],
                            capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return {"interpreter": interpreter, "state": "ok" if ok else "dead"}


def legacy_hook_files() -> List[Path]:
    """Pre-package hook scripts still on disk."""
    return [LEGACY_HOOKS_DIR / name for name in LEGACY_HOOK_FILES if (LEGACY_HOOKS_DIR / name).exists()]


def legacy_registrations() -> List[str]:
    """Matchers still registered with a pre-package script path."""
    pre = _pre_tool_use()
    found: List[str] = []
    for entry in pre if isinstance(pre, list) else []:
        inner = entry.get("hooks") if isinstance(entry, dict) else None
        if isinstance(inner, list) and any(_is_legacy(h) for h in inner):
            found.append(str(entry.get("matcher")))
    return found


def missing_damage_control_matchers() -> List[str]:
    """Damage-control matchers not registered in ``~/.claude/settings.json``.

    Checks the (matcher, kind) PAIR, mirroring ``register_damage_control_in_settings``'s dedup.
    Several matchers (Read/Grep/Glob) legitimately share one hook kind, so a kind-only check
    would treat all three as present the moment *any* one is registered — silently hiding a
    real gap in a security hook (e.g. Read guarded but Grep/Glob not).
    """
    pre = _pre_tool_use()
    return [m for m, kind in DAMAGE_CONTROL_MATCHERS.items() if not _owned_hook_entries(pre, m, kind)]


def _drifted(field_ok) -> List[str]:
    pre = _pre_tool_use()
    return [m for m, kind in DAMAGE_CONTROL_MATCHERS.items()
            if any(not field_ok(h) for h in _owned_hook_entries(pre, m, kind))]


def damage_control_timeout_drift() -> List[str]:
    """Registered damage-control matchers without the expected hook ``timeout`` (#1108).

    A registered hook with no (or a different) timeout runs under the harness
    default; the scan deadline is only known to fit inside ``HOOK_TIMEOUT_SECONDS``.
    """
    return _drifted(lambda h: h.get("timeout") == HOOK_TIMEOUT_SECONDS)


def damage_control_onfailure_drift() -> List[str]:
    """Registered damage-control matchers without ``onFailure: block`` (#1219).

    Without it a crash, timeout or missing interpreter lets the tool call run.
    """
    return _drifted(lambda h: h.get("onFailure") == "block")


def damage_control_registration_drift() -> List[str]:
    """Registered matchers whose interpreter or arguments differ from what this install would write.

    Compared against the RECORDED interpreter (not this process's), so ``doctor`` run from
    a different Python than the installer's still judges the registration it will meet.
    """
    interpreter = _read_install_record().get("interpreter")
    pre = _pre_tool_use()
    drifted = []
    for matcher, kind in DAMAGE_CONTROL_MATCHERS.items():
        for h in _owned_hook_entries(pre, matcher, kind):
            if h.get("command") != interpreter or h.get("args") != _hook_args(kind):
                drifted.append(matcher)
                break
    return drifted


def overlay_files() -> Dict[str, List[Path]]:
    """The host's own rule/tooldef files in the overlay dirs, which win over a bundled file of the same name."""
    return {
        "rules": sorted(p for p in (RULES_DIR.glob("*") if RULES_DIR.is_dir() else []) if p.suffix in (".yaml", ".json")),
        "tooldefs": sorted(p for p in (TOOLDEFS_DIR.glob("*") if TOOLDEFS_DIR.is_dir() else [])
                           if p.suffix in (".yaml", ".json")),
    }


def shadowed_bundled_files() -> Dict[str, List[str]]:
    """Overlay files that REPLACE a bundled file of the same name: ``{section: [stem, ...]}``."""
    from damage_control import groups

    bundled = groups.bundled_files()
    return {section: sorted(p.stem for p in files if p.stem in bundled[section])
            for section, files in overlay_files().items()}


def retire_pristine_overlay_files(log) -> Dict[str, List[str]]:
    """Remove overlay copies that are byte-identical to a version we ever shipped.

    Before the package, heal COPIED the bundled rules and tooldefs into the overlay dirs, and
    the hooks read those copies. The package now carries them, immutable, so a leftover copy
    is not a protection: it is a stale file that REPLACES the current bundled one of the same
    name. A copy matching a shipped hash holds nothing the bundle lacks and goes; anything
    else is a human's own edit and stays (it still overrides by name, as an edited file
    always did) and is reported (#1141, #916).
    """
    removed: Dict[str, List[str]] = {"rules": [], "tooldefs": []}
    kept: Dict[str, List[str]] = {"rules": [], "tooldefs": []}
    baselines = load_rule_baselines()
    for section, live_dir in (("rules", RULES_DIR), ("tooldefs", TOOLDEFS_DIR)):
        shipped = baselines.get(section, {})
        for name, hashes in sorted(shipped.items()):
            target = live_dir / name
            if not target.is_file():
                continue
            if _sha256(target) in hashes:
                target.unlink()
                removed[section].append(name)
                log(f"✓ Removed {section[:-1]} copy {name} (the package ships it now)")
            else:
                kept[section].append(name)
                log(f"⚠️  Kept {section[:-1]} {name}: it was edited, so it replaces the bundled file of "
                    "that name. Delete it to use the bundled one.")
    return {"rules_retired": removed["rules"], "tooldefs_retired": removed["tooldefs"],
            "rules_kept_edited": kept["rules"], "tooldefs_kept_edited": kept["tooldefs"]}


def install_breakglass() -> Path:
    """Copy the stdlib-only break-glass script out of the venv, to where a broken venv cannot hide it."""
    source = Path(__file__).parent / "safety" / "breakglass.py"
    target = BREAKGLASS_DIR / "damage-control-breakglass"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(source.read_bytes())
    tmp.chmod(0o755)
    tmp.replace(target)
    return target


def remove_legacy_hook_files(log) -> List[str]:
    """Delete the pre-package hook scripts, by name. Only called once the package hooks are registered and proven."""
    removed = []
    for path in legacy_hook_files():
        path.unlink()
        removed.append(path.name)
        log(f"✓ Removed legacy hook script {path.name}")
    try:
        LEGACY_HOOKS_DIR.rmdir()
    except OSError:
        pass  # not empty, or already gone: a file we do not own is not ours to remove
    return removed


def heal_damage_control(
    quiet: bool = False,
    force: bool = False,
    allow_foreign: bool = False,
) -> Dict[str, Any]:
    """Make this machine's damage control match the installed package. Non-interactive.

    **Provenance (#936).** Machine-global files are written only from the canonically
    installed tool. A checkout that is not the installed tool refuses outright rather than
    registering its own interpreter and profile (a worktree's venv is deleted with the
    worktree, and ``onFailure: block`` would then refuse every tool call).

    **Prove, then switch.** The package hooks are exercised exactly as registered
    (:func:`damage_control_selftest`) before the registration changes. On a failed selftest
    the existing registration is left alone, so an install that cannot work never replaces
    one that does (#1219). Replacing the pre-package registrations and adding the new ones is
    ONE atomic settings write; the old scripts are deleted only after it lands.

    The rules and tooldefs are not installed: the package ships them. What heal does with
    them is retire the stale copies older versions made (:func:`retire_pristine_overlay_files`).

    ``force`` is accepted for the CLI's sake and means nothing now that no installed file is
    overwritten. Returns a summary dict of what changed.
    """
    log = (lambda *a: None) if quiet else print

    summary: Dict[str, Any] = {
        "rules_retired": [], "tooldefs_retired": [], "rules_kept_edited": [], "tooldefs_kept_edited": [],
        "packs_migrated": [], "profile_written": "", "legacy_hooks_removed": [],
        "matchers_added": 0, "policy_scaffolded": False, "settings_error": None,
        "selftest_failures": [], "claude_code_version": "", "interpreter": "",
        "breakglass": "", "refused": False, "provenance": "",
    }

    from agentwire.safety import host_profile
    from agentwire.safety import provenance as prov

    state, canonical, running = prov.install_provenance()
    summary["provenance"] = state
    if state in prov.REFUSING_STATES and not allow_foreign:
        summary["refused"] = True
        for line in prov.refusal_lines(canonical, running, "damage-control hooks/rules"):
            log(line)
        return summary

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Host-owned policy file: create with `enabled: true` (fail-secure) if absent.
    # Never overwrite an existing one — it carries the owner's kill-switch state.
    if scaffold_damagecontrol_file():
        summary["policy_scaffolded"] = True
        log(f"✓ Scaffolded {DAMAGECONTROL_FILE} (enabled: true)")

    pack_state = migrate_pack_state()
    summary["packs_migrated"] = pack_state["migrated"]
    if pack_state["migrated"]:
        log(f"✓ Recorded already-installed packs as enabled: {', '.join(pack_state['migrated'])}")

    summary.update(retire_pristine_overlay_files(log))

    interpreter = sys.executable
    summary["interpreter"] = interpreter
    summary["profile_written"] = str(host_profile.write_manifest())
    summary["breakglass"] = str(install_breakglass())

    # Fail closed only on a hook set that demonstrably works: a block on top of a
    # broken install turns a quiet gap into a bricked session (#1219).
    selftest_failures = damage_control_selftest(interpreter)
    summary["selftest_failures"] = selftest_failures
    if selftest_failures:
        log("⚠️  Damage-control selftest FAILED -- registration left as it was, nothing switched:")
        for line in selftest_failures:
            log(f"     {line}")
        return summary

    version = claude_code_version()
    summary["claude_code_version"] = _version_text(version)
    if onfailure_supported(version) is not True:
        log(f"⚠️  Claude Code {_version_text(version)} predates onFailure support "
            f"(>= {_version_text(ONFAILURE_MIN_CLAUDE_VERSION)}): the guard FAILS OPEN "
            "if a hook crashes, times out or cannot start. Upgrade Claude Code.")
    try:
        added = register_damage_control_in_settings(interpreter, fail_closed=True)
    except SettingsError as e:
        summary["settings_error"] = str(e)
        log(f"⚠️  {e}")
        return summary
    summary["matchers_added"] = added
    if added:
        log(f"✓ Registered or updated {added} damage-control matcher entr{'ies' if added != 1 else 'y'} "
            f"(interpreter {interpreter})")
    write_install_record(interpreter)
    summary["legacy_hooks_removed"] = remove_legacy_hook_files(log)
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
    from damage_control.engine import DEFAULT_UNATTENDED_ALLOW
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

    With ``--yes``/``assume_yes`` it runs unattended; without it, the same heal runs behind
    two interactive confirmations. Registers the package hooks (after proving them),
    retires stale rule copies, writes the profile and the break-glass script. Returns 1
    when anything was refused or left unproven, so a script can never read a refusal as
    success — which is the whole of #936's operator picture.
    """
    print("AgentWire Safety Installation")
    print("=" * 50)
    print()

    if not assume_yes:
        print("This will register the damage control security hooks in ~/.claude/settings.json:")
        print(f"  {sys.executable} -I -m {HOOK_MODULE} <bash|edit|write|read|mcp>")
        print()
        print("The hooks will:")
        print("  • Block dangerous commands (rm -rf /, etc.)")
        print("  • Protect sensitive files (.env, SSH keys, etc.)")
        print("  • Log all security decisions")
        print()

        if input("Proceed with installation? [y/N] ").strip().lower() != "y":
            print("Installation cancelled.")
            return 0

    print()
    summary = heal_damage_control(force=force, allow_foreign=allow_foreign)

    if summary["refused"] or summary["settings_error"] or summary["selftest_failures"]:
        return 1

    touched = any(summary[k] for k in (
        "rules_retired", "tooldefs_retired", "legacy_hooks_removed", "matchers_added", "policy_scaffolded"))
    held_back = summary["rules_kept_edited"] or summary["tooldefs_kept_edited"]
    print()
    print("✓ Damage control synced." if touched else "✓ Damage control already in sync — nothing to do.")
    print(f"  Break-glass if the venv ever breaks: python3 {summary['breakglass']} unregister")
    if held_back:
        print("⚠️  Edited rule/tooldef overlays replace bundled files of the same name (see above).")
    print()
    print("Next steps:")
    print("  1. Test with: agentwire safety check 'rm -rf /'")
    print("  2. View status: agentwire safety status")
    print("  3. Add tool rules: ~/.agentwire/damage-control/<tool>.yaml")
    print("  4. View tool commands: agentwire safety tooldefs list")
    return 0
