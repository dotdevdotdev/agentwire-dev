"""Suggest ``delete-recursive`` entries from the audit log (#1152).

Read-only: it counts the recursive removals damage control blocked, keeps the
ones aimed at regenerable-looking directories, and PRINTS the policy a human
could paste. It never writes policy — the policy files are the protected
control plane, edited by the user.
"""

import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentwire.safety._core import _abs_path, _operand_is_literal, _parse_rm_command
from agentwire.safety.audit import load_rows

REGENERABLE_NAMES = (
    "dist", "build", "node_modules", ".next", ".nuxt", "target", "__pycache__",
    ".pytest_cache", ".turbo", "coverage", ".cache", "out",
)


def _project_of(cwd: str) -> str:
    """The git root above ``cwd`` when it still exists, else ``cwd`` itself."""
    current = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(cwd)
        current = parent


def blocked_regenerable_targets(rows: List[dict], stats: Optional[Dict[str, int]] = None) -> Counter:
    """``Counter[(project, name)]`` over blocked Bash `rm` rows aimed at regenerable directories.

    A blocked Bash row whose ``command``/``cwd`` is not a string is skipped and
    counted in ``stats["unreadable"]``.
    """
    counts: Counter = Counter()
    for row in rows:
        if row.get("kind") == "outcome" or row.get("decision") != "blocked" or row.get("tool") != "Bash":
            continue
        cwd = row.get("cwd")
        command = row.get("command")
        if not isinstance(cwd, str) or not isinstance(command, str):
            if stats is not None:
                stats["unreadable"] = stats.get("unreadable", 0) + 1
            continue
        if not cwd or "rm" not in command:
            continue
        try:
            parsed = _parse_rm_command(command, cwd)
        except Exception:
            continue
        for flags, operands, directory in parsed or []:
            for tok in operands:
                if not _operand_is_literal(tok):
                    continue
                name = os.path.basename(_abs_path(tok, directory).rstrip("/"))
                if name in REGENERABLE_NAMES:
                    counts[(_project_of(directory), name)] += 1
    return counts


def snippet_for(names: List[str]) -> str:
    """The YAML a user pastes into a project's ``.damagecontrol.yml``."""
    lines = ["allowed_paths:"]
    for name in names:
        lines += [f'  - path: "{name}/*"', "    allow: [delete-recursive]"]
    return "\n".join(lines)


def suggest_delete_recursive(
    days: int = 14, min_count: int = 2, log_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """Recurring blocked removals of regenerable directories, per project, with the snippet to paste."""
    stats: Dict[str, int] = {}
    counts = blocked_regenerable_targets(load_rows(log_dir, days, stats), stats)
    by_project: Dict[str, Dict[str, int]] = {}
    for (project, name), n in counts.items():
        if n >= min_count:
            by_project.setdefault(project, {})[name] = n
    suggestions = []
    for project, names in sorted(by_project.items(), key=lambda kv: -sum(kv[1].values())):
        ordered = sorted(names, key=lambda k: -names[k])
        suggestions.append({
            "project": project,
            "file": str(Path(project) / ".damagecontrol.yml"),
            "targets": {k: names[k] for k in ordered},
            "snippet": snippet_for(ordered),
        })
    return {"days": days, "min_count": min_count, "suggestions": suggestions, "unreadable": stats.get("unreadable", 0)}


def format_suggestions(result: Dict[str, Any]) -> str:
    text = _format_suggestions(result)
    if result.get("unreadable"):
        text += f"\n\n{result['unreadable']} unreadable rows skipped."
    return text


def _format_suggestions(result: Dict[str, Any]) -> str:
    if not result["suggestions"]:
        return (
            f"No regenerable directory was blocked {result['min_count']}+ times "
            f"in the last {result['days']} days — nothing to suggest."
        )
    lines = [
        f"Blocked `rm -rf` of regenerable directories — last {result['days']} days "
        f"(seen {result['min_count']}+ times):",
    ]
    for s in result["suggestions"]:
        lines += ["", f"{s['project']}"]
        lines += [f"  {n:4d}  {name}" for name, n in s["targets"].items()]
        lines += ["", f"  To allow them, add to {s['file']} (a human edit — this command never writes policy):", ""]
        lines += [f"    {line}" for line in s["snippet"].splitlines()]
    lines += [
        "",
        "Or, for every project: put `*/<name>/*` entries (e.g. `*/node_modules/*`) in ~/.agentwire/damagecontrol.yml.",
        "`allow: all` does NOT include delete-recursive; it must be named.",
    ]
    return "\n".join(lines)
