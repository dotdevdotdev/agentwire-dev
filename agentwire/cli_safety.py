"""Safety CLI commands for AgentWire damage control integration.

This module is the CLI front end. All decision logic — pattern matching,
allowlist evaluation, the decision ladder — lives in ``agentwire.safety._core``,
which is also inlined into the bundled hook scripts. See #164 for the dedup
history and ``scripts/regen_damage_control_hooks.py`` for how the hook scripts
stay in sync.
"""

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None

from agentwire.safety._core import (
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

# Backwards-compat alias used by older tests
_match_allowed_path = match_path


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

    Invoked fire-and-forget by the bash damage-control hook when an
    unattended (scheduler) run hits an ``ask``-tier command that isn't on the
    allowlist. Emails the owner via the shared Resend wiring so the blocked
    action surfaces immediately instead of silently disappearing.
    """
    session = os.environ.get("AGENTWIRE_SESSION_NAME") or os.environ.get(
        "AGENTWIRE_SESSION_ID", "unknown"
    )
    cwd = os.environ.get("PWD", "")
    subject = f"[agentwire] Unattended action blocked: {reason[:80]}"
    body = (
        f"An unattended (scheduled) agent attempted an action that requires "
        f"human confirmation, so it was **blocked** (fail-closed).\n\n"
        f"- **Session:** {session}\n"
        f"- **Working dir:** {cwd or 'unknown'}\n"
        f"- **Rule:** {rule_id or 'unknown'}\n"
        f"- **Reason:** {reason}\n"
        f"- **Command:** `{command}`\n"
        f"- **When:** {datetime.now().isoformat(timespec='seconds')}\n\n"
        f"Nothing was executed. To permit this specific action for the task in "
        f"future, add rule id `{rule_id}` to that task's `unattended_allow` in "
        f"its `.agentwire.yml` (or to `safety.unattended_allow` globally)."
    )
    try:
        from agentwire.channels.email import send_email

        result = send_email(subject=subject, body=body)
        if getattr(result, "success", False):
            return 0
        print(f"notify-unattended-block: email failed: "
              f"{getattr(result, 'error', 'unknown')}", file=sys.stderr)
        return 1
    except Exception as e:  # email not configured, etc. — never crash the notifier
        print(f"notify-unattended-block: {e}", file=sys.stderr)
        return 1


def safety_logs_cmd(
    tail: Optional[int] = None,
    session: Optional[str] = None,
    today: bool = False,
    pattern: Optional[str] = None,
) -> int:
    """CLI command: ``agentwire safety logs``."""
    print(format_audit_logs(query_audit_logs(tail, session, today, pattern)))
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
    "mcp__agentwire__(email_send|quo_send)": "mcp-tool-damage-control.py",
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
        already = any(
            h.get("command") == command
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
    """
    if not source.exists():
        return "ok"
    if not target.exists():
        return "missing"
    try:
        return "ok" if target.read_bytes() == source.read_bytes() else "stale"
    except OSError:
        return "stale"


def damage_control_hook_drift() -> Dict[str, str]:
    """Drift state per DC hook script (``bash/edit/write/mcp-tool-...`` + logger).

    Returns ``{filename: missing|stale|ok}`` comparing the installed copies in
    ``~/.agentwire/hooks/damage-control/`` against the bundled package source.
    """
    hooks_source = Path(__file__).parent / "hooks" / "damage-control"
    return {
        fn: _file_drift_state(HOOKS_DIR / fn, hooks_source / fn)
        for fn in DAMAGE_CONTROL_FILES
    }


def rules_drift() -> Dict[str, str]:
    """Drift state per bundled rule file vs ``~/.agentwire/damage-control/``.

    Only the bundled rule set is inspected (user-added rules with no bundled
    counterpart are not "drift"). Returns ``{filename: missing|stale|ok}``.
    """
    try:
        source_dir = get_damage_control_source()
    except FileNotFoundError:
        return {}
    return {
        src.name: _file_drift_state(RULES_DIR / src.name, src)
        for src in sorted(source_dir.glob("*.yaml"))
    }


def missing_damage_control_matchers() -> List[str]:
    """Damage-control matchers not registered in ``~/.claude/settings.json``."""
    settings_file = Path.home() / ".claude" / "settings.json"
    try:
        settings = json.loads(settings_file.read_text())
    except (OSError, json.JSONDecodeError):
        settings = {}
    pre = settings.get("hooks", {}).get("PreToolUse", []) if isinstance(settings, dict) else []
    registered = {
        h.get("command")
        for entry in pre
        for h in (entry.get("hooks", []) if isinstance(entry, dict) else [])
    }
    missing = []
    for matcher, hook_file in DAMAGE_CONTROL_MATCHERS.items():
        if f"~/.agentwire/hooks/damage-control/{hook_file}" not in registered:
            missing.append(matcher)
    return missing


def heal_damage_control(quiet: bool = False) -> Dict[str, Any]:
    """Drift-aware sync of DC hook scripts, rules, tooldefs, and matchers.

    Non-interactive. Agentwire-owned hook scripts are installed when missing and
    overwritten when stale (they carry no user edits). Rules and tooldefs are
    install-missing-only — an existing rule, including a hand-customized one, is
    left untouched, so this never blind-clobbers user rules. Matchers are
    registered if absent. Returns a summary dict of what changed.
    """
    log = (lambda *a: None) if quiet else print

    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    TOOLDEFS_DIR.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "hooks_installed": [], "hooks_updated": [],
        "rules_installed": [], "tooldefs_installed": [], "matchers_added": 0,
        "policy_scaffolded": False,
    }

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
        shutil.copy2(src, target)
        if fn.endswith(".py"):
            target.chmod(0o755)
        if state == "missing":
            summary["hooks_installed"].append(fn)
            log(f"✓ Installed hook {fn}")
        else:
            summary["hooks_updated"].append(fn)
            log(f"✓ Updated stale hook {fn}")

    # Rules: install missing only — never overwrite an existing (possibly
    # hand-customized) rule file.
    try:
        rules_source = get_damage_control_source()
        for src in sorted(rules_source.glob("*.yaml")):
            target = RULES_DIR / src.name
            if not target.exists():
                shutil.copy2(src, target)
                summary["rules_installed"].append(src.name)
                log(f"✓ Installed rule {src.name}")
    except FileNotFoundError as e:
        log(f"⚠️  {e}")

    # Tooldefs: install missing only (user overrides win, same as rules).
    try:
        tooldefs_source = get_tooldefs_source()
        for src in sorted(tooldefs_source.glob("*.yaml")):
            target = TOOLDEFS_DIR / src.name
            if not target.exists():
                shutil.copy2(src, target)
                summary["tooldefs_installed"].append(src.name)
                log(f"✓ Installed tooldef {src.name}")
    except FileNotFoundError as e:
        log(f"⚠️  {e}")

    added = register_damage_control_in_settings()
    summary["matchers_added"] = added
    if added:
        log(f"✓ Registered {added} damage-control matcher{'s' if added != 1 else ''}")

    return summary


def safety_install_cmd(assume_yes: bool = False) -> int:
    """CLI command: ``agentwire safety install``.

    With ``--yes``/``assume_yes`` it runs unattended and drift-aware: installs
    missing hook scripts/rules/tooldefs, updates stale *owned* hook scripts, and
    registers any absent matchers, without prompting and without clobbering an
    existing (possibly customized) rule. Without it, the same heal runs behind
    the two interactive confirmations.
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
    summary = heal_damage_control()

    touched = (
        summary["hooks_installed"] or summary["hooks_updated"]
        or summary["rules_installed"] or summary["tooldefs_installed"]
        or summary["matchers_added"] or summary["policy_scaffolded"]
    )
    print()
    if touched:
        print("✓ Damage control synced.")
    else:
        print("✓ Damage control already in sync — nothing to do.")
    print()
    print("Next steps:")
    print("  1. Test with: agentwire safety check 'rm -rf /'")
    print("  2. View status: agentwire safety status")
    print("  3. Add tool rules: ~/.agentwire/damage-control/<tool>.yaml")
    print("  4. View tool commands: agentwire safety tooldefs list")
    return 0
