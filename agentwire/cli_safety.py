"""Safety CLI commands for AgentWire damage control integration."""

import fnmatch
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


# Default config directory
CONFIG_DIR = Path.home() / ".agentwire"
HOOKS_DIR = CONFIG_DIR / "hooks" / "damage-control"
LOGS_DIR = CONFIG_DIR / "logs" / "damage-control"
RULES_DIR = CONFIG_DIR / "damage-control"
TOOLDEFS_DIR = CONFIG_DIR / "tooldefs"

# Files to install from the package (hook scripts only, not rules)
DAMAGE_CONTROL_FILES = [
    "bash-tool-damage-control.py",
    "edit-tool-damage-control.py",
    "write-tool-damage-control.py",
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


def is_glob_pattern(pattern: str) -> bool:
    """Check if a pattern contains glob wildcards."""
    return '*' in pattern or '?' in pattern or '[' in pattern


def glob_to_regex(pattern: str) -> str:
    """Convert a glob pattern to a regex pattern."""
    result = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == '*':
            result += '.*'
        elif c == '?':
            result += '.'
        elif c == '[':
            j = i + 1
            while j < len(pattern) and pattern[j] != ']':
                j += 1
            result += pattern[i:j+1]
            i = j
        elif c in '.^$+{}|()\\':
            result += '\\' + c
        else:
            result += c
        i += 1
    return result


def matches_path_in_command(pattern: str, command: str) -> bool:
    """Check if a path pattern occurs in command, restricted to file-path contexts."""
    expanded = os.path.expanduser(pattern)

    if not is_glob_pattern(pattern):
        return expanded in command

    glob_regex = glob_to_regex(expanded)
    # Only match if surrounded by path-context chars (not e.g. inside `module.method()`)
    file_path_regex = r'(?:^|[\s/="\'<>])' + glob_regex + r'(?:[\s"\')<>]|$)'

    try:
        if not re.search(file_path_regex, command, re.IGNORECASE):
            return False
    except re.error:
        return False

    # Reject method-call lookalikes: `foo.py(` should not match `*.py`
    extension = pattern.split('*')[-1] if '*' in pattern else pattern
    if extension.startswith('.'):
        extension = extension[1:]
    if extension and re.search(r'\w\.' + re.escape(extension) + r'\s*\(', command):
        return False

    return True


ALL_OPERATIONS = {"read", "write", "edit", "delete", "move", "chmod"}


def _parse_allowed_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Parse an allowed-path entry to {path: str, allow: set}.

    Entry must be a dict with "path" key and optional "allow" (defaults to "all").
    """
    allow = entry.get("allow", "all")
    if isinstance(allow, str) and allow.strip().lower() == "all":
        return {"path": entry["path"], "allow": ALL_OPERATIONS.copy()}
    if isinstance(allow, list):
        return {"path": entry["path"], "allow": {a.strip().lower() for a in allow}}
    if isinstance(allow, str):
        return {"path": entry["path"], "allow": {allow.strip().lower()}}
    return {"path": entry["path"], "allow": ALL_OPERATIONS.copy()}


def _find_project_config_for_safety() -> tuple:
    """Walk up from $PWD to find .agentwire.yml and return (project_root, allowed_paths)."""
    cwd = os.environ.get("PWD", os.getcwd())
    current = os.path.abspath(cwd)
    while True:
        config_file = os.path.join(current, ".agentwire.yml")
        if os.path.isfile(config_file):
            try:
                if yaml:
                    with open(config_file, "r") as f:
                        data = yaml.safe_load(f) or {}
                    safety = data.get("safety", {})
                    if isinstance(safety, dict):
                        paths = safety.get("allowed_paths", [])
                        if isinstance(paths, list):
                            return current, paths
            except Exception:
                pass
            return current, []
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return cwd, []


def load_allowed_paths(patterns: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Load allowed paths from patterns config and per-project .agentwire.yml.

    Returns list of {"path": str, "allow": set} entries.
    """
    raw = list(patterns.get("allowedPaths", []))

    project_root, project_paths = _find_project_config_for_safety()
    for p in project_paths:
        if not isinstance(p, dict):
            continue
        entry = _parse_allowed_entry(p)
        if not os.path.isabs(os.path.expanduser(entry["path"])):
            entry["path"] = os.path.join(project_root, entry["path"])
        raw.append(entry)

    # Parse all entries (skip non-dict items)
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if "allow" in item and isinstance(item["allow"], set):
            result.append(item)
        else:
            result.append(_parse_allowed_entry(item))
    return result


def _match_allowed_path(file_path: str, pattern: str) -> bool:
    """Check if file_path matches an allowed-path pattern (glob or prefix)."""
    expanded_pattern = os.path.expanduser(pattern)
    normalized = os.path.normpath(file_path)
    expanded_normalized = os.path.expanduser(normalized)

    if is_glob_pattern(pattern):
        lower_path = expanded_normalized.lower()
        lower_pattern = expanded_pattern.lower()
        if fnmatch.fnmatch(lower_path, lower_pattern):
            return True
        basename = os.path.basename(expanded_normalized)
        if fnmatch.fnmatch(basename.lower(), lower_pattern):
            return True
        return False
    else:
        if expanded_normalized.startswith(expanded_pattern) or expanded_normalized == expanded_pattern.rstrip('/'):
            return True
        return False


def is_path_allowed_for_op(file_path: str, allowed_paths: List[Dict[str, Any]], operation: str) -> bool:
    """Check if file_path has the given operation permitted by any allowed-path entry."""
    for entry in allowed_paths:
        if _match_allowed_path(file_path, entry["path"]):
            if operation in entry["allow"]:
                return True
    return False


def _extract_command_paths(command: str) -> List[str]:
    """Extract file/directory paths from a command string."""
    paths = []
    for m in re.finditer(r'''["']([^"']+)["']''', command):
        candidate = m.group(1)
        if '/' in candidate or candidate.startswith('.'):
            paths.append(os.path.expanduser(candidate))
    for token in re.split(r'\s+', command):
        token = token.strip("\"'")
        if token.startswith(('/', '~/', './')):
            paths.append(os.path.expanduser(token))
        elif '/' in token and not token.startswith('-'):
            paths.append(os.path.expanduser(token))
    return paths


def is_command_path_allowed(command: str, allowed_paths: List[Dict[str, Any]], operation: str = "write") -> bool:
    """Check if ALL file paths in a command have the required operation permitted.

    Security: ALL paths must match (not any). This prevents commands like
    `rm /tmp/safe.txt /etc/passwd` from being allowed just because /tmp is allowlisted.
    """
    if not allowed_paths:
        return False
    paths = _extract_command_paths(command)
    if not paths:
        return False
    for p in paths:
        if not is_path_allowed_for_op(p, allowed_paths, operation):
            return False
    return True


def _infer_operation_from_reason(reason: str) -> str:
    """Infer the required operation from a bash pattern's reason field."""
    reason_lower = reason.lower()
    if "rm" in reason_lower or "delet" in reason_lower or "trash" in reason_lower or "rmdir" in reason_lower:
        return "delete"
    if "chmod" in reason_lower or "permission" in reason_lower:
        return "chmod"
    if "chown" in reason_lower or "chgrp" in reason_lower:
        return "chmod"
    if "mv" in reason_lower or "move" in reason_lower:
        return "move"
    return "write"


def _cmd_to_regex(cmd: str) -> Optional[str]:
    """Convert a tooldef command string to a regex matching its fixed prefix.

    Stops at the first flag (- or --), placeholder (<...>), or optional ([...]).
    Example: "gws gmail +send --to <addr>" -> r'\\bgws\\s+gmail\\s+\\+send\\b'
    """
    tokens = cmd.strip().split()
    fixed = []
    for token in tokens:
        if token.startswith('<') or token.startswith('[') or token.startswith('-'):
            break
        fixed.append(token)
    if not fixed:
        return None
    parts = [re.escape(t) for t in fixed]
    pattern = r'\b' + r'\s+'.join(parts)
    last = fixed[-1]
    if last and (last[-1].isalnum() or last[-1] == '_'):
        pattern += r'\b'
    return pattern


def _load_write_patterns_from_tooldefs() -> List[Dict[str, Any]]:
    """Generate ask:true patterns from write-tier commands in tooldefs."""
    if not yaml:
        return []
    patterns = []
    tooldefs_source = TOOLDEFS_DIR if TOOLDEFS_DIR.exists() else None
    if tooldefs_source is None:
        try:
            tooldefs_source = get_tooldefs_source()
        except FileNotFoundError:
            return patterns
    for tf_file in sorted(tooldefs_source.glob("*.yaml")):
        try:
            with open(tf_file, "r") as f:
                data = yaml.safe_load(f) or {}
            tool_name = data.get("name", tf_file.stem)
            for cmd_def in data.get("commands", []):
                if cmd_def.get("access") != "write":
                    continue
                cmd_str = cmd_def.get("cmd", "")
                regex = _cmd_to_regex(cmd_str)
                if not regex:
                    continue
                patterns.append({
                    "pattern": regex,
                    "reason": f"{tool_name}: {cmd_def.get('description', cmd_str)}",
                    "ask": True,
                })
        except Exception:
            continue
    return patterns


def load_patterns() -> Dict[str, Any]:
    """Load and merge patterns from all .yaml files in RULES_DIR,
    then append ask:true patterns generated from write-tier tooldef commands."""
    if not yaml:
        print("Error: PyYAML not installed.", file=sys.stderr)
        return {}

    if not RULES_DIR.exists():
        return {}

    merged = {
        "bashToolPatterns": [],
        "zeroAccessPaths": [],
        "readOnlyPaths": [],
        "noDeletePaths": [],
        "allowedPaths": [],
    }

    yaml_files = sorted(RULES_DIR.glob("*.yaml"))
    for rules_file in yaml_files:
        try:
            with open(rules_file, "r") as f:
                data = yaml.safe_load(f) or {}
            for key in merged:
                merged[key].extend(data.get(key, []))
        except Exception as e:
            print(f"Warning: Could not load {rules_file.name}: {e}", file=sys.stderr)

    # Append write-tier tooldef patterns after rules (so explicit blocks take precedence)
    merged["bashToolPatterns"].extend(_load_write_patterns_from_tooldefs())

    return merged


def check_command_safety(command: str, verbose: bool = False) -> Dict[str, Any]:
    """
    Dry-run check if a command would be blocked/allowed/asked.

    Returns dict with:
        - decision: "allow" | "block" | "ask"
        - reason: string description
        - pattern: matched pattern (if any)

    Evaluation order:
      1. Hard-blocked bash patterns (bypassable=false or unset) → BLOCK always
      2. Ask patterns → ASK always
      3. Bypassable bash patterns → check if ALL target paths have the required permission
      4. zeroAccessPaths → check allowlist with "read" permission
      5. readOnlyPaths → check allowlist with operation-specific permission
      6. noDeletePaths → check allowlist with "delete" permission
    """
    patterns = load_patterns()
    allowed = load_allowed_paths(patterns)

    bash_patterns = patterns.get("bashToolPatterns", [])
    bypassable_matches = []

    for pattern_obj in bash_patterns:
        if not isinstance(pattern_obj, dict):
            continue

        pattern = pattern_obj.get("pattern", "")
        reason = pattern_obj.get("reason", "Matched pattern")
        should_ask = pattern_obj.get("ask", False)
        bypassable = pattern_obj.get("bypassable", False)

        try:
            if re.search(pattern, command, re.IGNORECASE):
                if should_ask:
                    return {
                        "decision": "ask",
                        "reason": reason,
                        "pattern": pattern,
                        "command": command
                    }
                elif bypassable:
                    bypassable_matches.append((pattern, reason))
                else:
                    return {
                        "decision": "block",
                        "reason": reason,
                        "pattern": pattern,
                        "command": command
                    }
        except re.error:
            if verbose:
                print(f"Warning: Invalid regex pattern: {pattern}", file=sys.stderr)

    # Bypassable matches: block unless every targeted path is in the allowlist
    for pattern, reason in bypassable_matches:
        operation = _infer_operation_from_reason(reason)
        if not is_command_path_allowed(command, allowed, operation):
            return {
                "decision": "block",
                "reason": reason,
                "pattern": pattern,
                "command": command
            }

    # Check path-based restrictions
    zero_access = patterns.get("zeroAccessPaths", [])
    read_only = patterns.get("readOnlyPaths", [])
    no_delete = patterns.get("noDeletePaths", [])

    for path in zero_access:
        if matches_path_in_command(path, command):
            if is_command_path_allowed(command, allowed, "read"):
                continue
            return {
                "decision": "block",
                "reason": f"Zero-access path: {path}",
                "pattern": f"zeroAccessPath: {path}",
                "command": command
            }

    for path in read_only:
        if matches_path_in_command(path, command) and any(op in command for op in ["rm", "mv", "sed -i", ">"]):
            # Infer operation
            op = "write"
            if "rm" in command:
                op = "delete"
            elif "mv" in command:
                op = "move"
            if is_command_path_allowed(command, allowed, op):
                continue
            return {
                "decision": "block",
                "reason": f"Read-only path: {path}",
                "pattern": f"readOnlyPath: {path}",
                "command": command
            }

    for path in no_delete:
        if matches_path_in_command(path, command) and "rm" in command:
            if is_command_path_allowed(command, allowed, "delete"):
                continue
            return {
                "decision": "block",
                "reason": f"No-delete path: {path}",
                "pattern": f"noDeletePath: {path}",
                "command": command
            }

    return {
        "decision": "allow",
        "reason": "No patterns matched",
        "pattern": None,
        "command": command
    }


def get_safety_status() -> Dict[str, Any]:
    """Get current safety status - patterns count, recent blocks, etc."""
    patterns = load_patterns()

    rule_files = sorted(f.name for f in RULES_DIR.glob("*.yaml")) if RULES_DIR.exists() else []

    tooldefs_count = len(list(TOOLDEFS_DIR.glob("*.yaml"))) if TOOLDEFS_DIR.exists() else 0

    status = {
        "hooks_installed": HOOKS_DIR.exists(),
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
        "recent_blocks": []
    }

    # Count recent blocks from today's log
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
                status["recent_blocks"] = blocks[-5:]  # Last 5 blocks
            except Exception as e:
                status["error"] = f"Error reading logs: {e}"

    return status


def query_audit_logs(
    tail: Optional[int] = None,
    session: Optional[str] = None,
    today: bool = False,
    pattern: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Query audit logs with filters.

    Args:
        tail: Limit to last N entries
        session: Filter by session_id
        today: Only show today's entries
        pattern: Filter by pattern match (regex or substring)
    """
    if not LOGS_DIR.exists():
        return []

    entries = []

    # Determine which log files to read
    if today:
        log_files = [LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"]
    else:
        log_files = sorted(LOGS_DIR.glob("*.jsonl"), reverse=True)

    for log_file in log_files:
        if not log_file.exists():
            continue

        try:
            with open(log_file, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)

                        # Apply filters
                        if session and entry.get("session_id") != session:
                            continue

                        if pattern:
                            # Check if pattern matches command or blocked_by
                            cmd = entry.get("command", "")
                            blocked_by = entry.get("blocked_by", "")
                            if pattern.lower() not in cmd.lower() and pattern.lower() not in blocked_by.lower():
                                continue

                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue

    # Apply tail limit
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
        icon = "✓"
        color = "\033[32m"  # Green
    elif decision == "block":
        icon = "✗"
        color = "\033[31m"  # Red
    else:  # ask
        icon = "?"
        color = "\033[33m"  # Yellow

    reset = "\033[0m"

    lines = []
    lines.append(f"{color}{icon} Decision: {decision.upper()}{reset}")
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

        # Color code by decision
        if decision == "blocked":
            color = "\033[31m"  # Red
        elif decision == "asked":
            color = "\033[33m"  # Yellow
        else:
            color = "\033[32m"  # Green
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
    """CLI command: agentwire safety check"""
    result = check_command_safety(command, verbose)
    print(format_check_result(result))
    return 0 if result["decision"] == "allow" else 1


def safety_status_cmd() -> int:
    """CLI command: agentwire safety status"""
    status = get_safety_status()
    print(format_safety_status(status))
    return 0


def safety_logs_cmd(
    tail: Optional[int] = None,
    session: Optional[str] = None,
    today: bool = False,
    pattern: Optional[str] = None
) -> int:
    """CLI command: agentwire safety logs"""
    entries = query_audit_logs(tail, session, today, pattern)
    print(format_audit_logs(entries))
    return 0


def safety_tooldefs_list_cmd() -> int:
    """CLI command: agentwire safety tooldefs list"""
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
    """CLI command: agentwire safety tooldefs show <tool>"""
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

    green = "\033[32m"
    yellow = "\033[33m"
    red = "\033[31m"
    reset = "\033[0m"

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


def safety_install_cmd() -> int:
    """CLI command: agentwire safety install - interactive setup"""
    print("AgentWire Safety Installation")
    print("=" * 50)
    print()

    # Check if already installed
    if HOOKS_DIR.exists() and RULES_DIR.exists() and any(RULES_DIR.glob("*.yaml")):
        print("⚠️  Safety hooks already installed")
        print(f"   Location: {HOOKS_DIR}")
        response = input("Reinstall? [y/N] ").strip().lower()
        if response != "y":
            print("Installation cancelled.")
            return 0

    print("This will install damage control security hooks to:")
    print(f"  {HOOKS_DIR}")
    print()
    print("The hooks will:")
    print("  • Block dangerous commands (rm -rf /, etc.)")
    print("  • Protect sensitive files (.env, SSH keys, etc.)")
    print("  • Log all security decisions")
    print()

    response = input("Proceed with installation? [y/N] ").strip().lower()
    if response != "y":
        print("Installation cancelled.")
        return 0

    # Find source files in package
    try:
        source_dir = get_damage_control_source()
    except FileNotFoundError as e:
        print(f"\n⚠️  {e}")
        print("   The damage-control hooks are missing from the package.")
        return 1

    # Create directories
    print()
    print("Creating directories...")
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    TOOLDEFS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"✓ Created {HOOKS_DIR}")
    print(f"✓ Created {LOGS_DIR}")
    print(f"✓ Created {RULES_DIR}")
    print(f"✓ Created {TOOLDEFS_DIR}")

    # Copy hook scripts from package to hooks directory
    hooks_source = Path(__file__).parent / "hooks" / "damage-control"
    print()
    print("Installing hooks...")
    for filename in DAMAGE_CONTROL_FILES:
        source_file = hooks_source / filename
        target_file = HOOKS_DIR / filename
        if source_file.exists():
            shutil.copy2(source_file, target_file)
            # Make scripts executable
            if filename.endswith(".py"):
                target_file.chmod(0o755)
            print(f"✓ Installed {filename}")
        else:
            print(f"⚠️  Missing {filename} in package")

    # Copy rules directory from package to user config
    print()
    print("Installing rules...")
    for rules_file in sorted(source_dir.glob("*.yaml")):
        target_file = RULES_DIR / rules_file.name
        shutil.copy2(rules_file, target_file)
        print(f"✓ Installed {rules_file.name}")

    # Copy tooldefs from package to user config
    print()
    print("Installing tooldefs...")
    try:
        tooldefs_source = get_tooldefs_source()
        for tooldef_file in sorted(tooldefs_source.glob("*.yaml")):
            target_file = TOOLDEFS_DIR / tooldef_file.name
            shutil.copy2(tooldef_file, target_file)
            print(f"✓ Installed {tooldef_file.name}")
    except FileNotFoundError as e:
        print(f"⚠️  {e}")

    print()
    print("✓ Installation complete!")
    print()
    print("Next steps:")
    print("  1. Test with: agentwire safety check 'rm -rf /'")
    print("  2. View status: agentwire safety status")
    print("  3. Add tool rules: ~/.agentwire/damage-control/<tool>.yaml")
    print("  4. View tool commands: agentwire safety tooldefs list")

    return 0
