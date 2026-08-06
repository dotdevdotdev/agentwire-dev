"""Authoring-time lint: will this task's posture refuse what the task says to do?

Two scheduled tasks failed on 2026-08-06 for the same reason — their prompts
specify work the unattended posture forbids (`memory-manager`'s children must
commit; `artifactsmmo`'s prompt says "commit and push directly to main" and it
shells out to `uv run`). Both surfaced as ``incomplete — exceeded
max_duration``, which points every investigation at the cap instead of the
cause (#914).

Nothing catches that when the task is WRITTEN, which is the one moment someone
is looking at it and can decide. This module cross-checks a task against the
live rule set and reports what its posture will refuse, for
``agentwire tasks review`` (before promote) and ``agentwire doctor`` (live
config).

The `pre:` / `post:` / `shell:` fields are checked EXACTLY — they are literal
commands. The `prompt:` can only be checked heuristically: it is English, and
the commands an agent will actually run are not knowable ahead of time. So the
prompt scan reports what it found and says it is a heuristic, rather than
implying the list is complete. A findings list that overstates its own
authority is the failure mode this whole issue is about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ._core import (
    check_command,
    load_config,
    parse_unattended_allow,
    resolve_unattended_grants,
)


@dataclass
class PostureFinding:
    """One command a task specifies that its unattended posture would refuse."""

    where: str          # "pre.commits", "prompt", "post[0]" …
    command: str
    rule_id: Optional[str]
    reason: str
    exact: bool         # False = extracted from prose, so possibly not a real command

    def render(self) -> str:
        hedge = "" if self.exact else " (from prompt text — heuristic)"
        rid = f" [{self.rule_id}]" if self.rule_id else ""
        return f"{self.where}: {self.command}{rid} — {self.reason}{hedge}"


@dataclass
class PostureReport:
    findings: list[PostureFinding] = field(default_factory=list)
    grant_errors: list[str] = field(default_factory=list)
    unknown_grants: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.findings or self.grant_errors or self.unknown_grants)


# Commands worth extracting from prose. Deliberately a small, high-signal list:
# a general "any word that could be a binary" scan would flood the report with
# false hits, and a lint nobody reads is worth less than no lint.
_PROMPT_TOOLS = (
    "git", "gh", "uv", "npm", "pnpm", "yarn", "cargo", "docker", "kubectl",
    "aws", "gcloud", "terraform", "helm", "supabase", "psql", "mysql",
    "agentwire", "make", "pip", "python", "ssh", "rsync", "curl",
)
_FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_TOOL_LINE_RE = re.compile(
    r"^\s*(?:\$\s*)?(" + "|".join(re.escape(t) for t in _PROMPT_TOOLS) + r")\s+\S.*$",
    re.MULTILINE,
)


def extract_prompt_commands(prompt: str) -> list[str]:
    """Best-effort: command-looking strings inside a task prompt.

    Reads fenced blocks, backticked spans, and bare lines starting with a known
    tool name. Deduplicated, order preserved.
    """
    candidates: list[str] = []
    for block in _FENCE_RE.findall(prompt or ""):
        candidates.extend(line for line in block.splitlines())
    candidates.extend(_BACKTICK_RE.findall(prompt or ""))
    for m in _TOOL_LINE_RE.finditer(prompt or ""):
        candidates.append(m.group(0))

    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        cmd = raw.strip().lstrip("$").strip()
        cmd = re.sub(r"^\s*#.*$", "", cmd).strip()
        if not cmd or cmd in seen:
            continue
        head = cmd.split()[0] if cmd.split() else ""
        if head not in _PROMPT_TOOLS:
            continue
        seen.add(cmd)
        out.append(cmd)
    return out


def _task_commands(task: Any) -> list[tuple[str, str, bool]]:
    """[(where, command, exact)] for every command a task specifies."""
    items: list[tuple[str, str, bool]] = []
    for pre in getattr(task, "pre", []) or []:
        if getattr(pre, "cmd", "").strip():
            items.append((f"pre.{pre.name}", pre.cmd.strip(), True))
        if getattr(pre, "validate", "") and str(pre.validate).strip():
            items.append((f"pre.{pre.name}.validate", str(pre.validate).strip(), True))
    for i, cmd in enumerate(getattr(task, "post", []) or []):
        if str(cmd).strip():
            items.append((f"post[{i}]", str(cmd).strip(), True))
    shell = getattr(task, "shell", None)
    if shell and str(shell).strip():
        items.append(("shell", str(shell).strip(), True))
    for cmd in extract_prompt_commands(getattr(task, "prompt", "") or ""):
        items.append(("prompt", cmd, False))
    for cmd in extract_prompt_commands(getattr(task, "on_task_end", "") or ""):
        items.append(("on_task_end", cmd, False))
    return items


def lint_task_posture(
    task: Any,
    config: dict,
    cwd: str,
    grants: Optional[dict] = None,
) -> PostureReport:
    """Report the commands ``task`` specifies that its unattended posture refuses.

    ``config`` is a loaded rule set (``load_config``) — the lint is only as
    truthful as the rules it is measured against, so callers pass the same set
    the hook will load, and the CLI prints which one that was.
    """
    report = PostureReport()

    entries = list(getattr(task, "unattended_allow", []) or [])
    task_grants, errors = parse_unattended_allow(entries)
    report.grant_errors = errors

    # A grant naming a rule id that does not exist grants nothing — and reads
    # exactly like one that works. That is how the install drift in #916 stayed
    # invisible: five of six DEFAULT_UNATTENDED_ALLOW ids name no live rule.
    known_ids = {
        p.get("id") for p in config.get("bashToolPatterns", []) if isinstance(p, dict)
    }
    report.unknown_grants = sorted(
        rid for rid in task_grants if rid and rid not in known_ids
    )

    if grants is None:
        merged = dict(resolve_unattended_grants(config))
        merged.update(task_grants)   # the task layer is the most specific
        grants = merged

    from ._core import unattended_grant_allows

    for where, command, exact in _task_commands(task):
        result = check_command(command, config)
        decision = result.get("decision")
        if decision == "block":
            report.findings.append(PostureFinding(
                where=where, command=command, rule_id=result.get("id"),
                reason=f"hard-blocked: {result.get('reason')}", exact=exact,
            ))
            continue
        if decision != "ask":
            continue
        allowed, why = unattended_grant_allows(
            result.get("id"), command, grants, cwd, pattern=result.get("pattern"),
        )
        if not allowed:
            report.findings.append(PostureFinding(
                where=where, command=command, rule_id=result.get("id"),
                reason=f"refused unattended: {why}", exact=exact,
            ))
    return report


def unattended_defaults_missing(config: dict) -> list[str]:
    """``DEFAULT_UNATTENDED_ALLOW`` ids that name no rule in ``config``.

    A non-empty result means the built-in grants are silently inert on this
    install — the tooldef drift behind #914's motivating failure (#916).
    """
    from ._core import DEFAULT_UNATTENDED_ALLOW

    known = {
        p.get("id") for p in config.get("bashToolPatterns", []) if isinstance(p, dict)
    }
    defaults, _ = parse_unattended_allow(DEFAULT_UNATTENDED_ALLOW)
    return sorted(rid for rid in defaults if rid not in known)


def load_effective_config(
    rules_dir: Optional[Path] = None,
    tooldefs_dir: Optional[Path] = None,
) -> tuple[dict, str]:
    """Load the rule set the hook would load, plus a label naming its sources.

    The label is not decoration: `~/.agentwire/damage-control/` and
    `~/.agentwire/tooldefs/` win over the bundled copies and never self-heal, so
    a lint result is only meaningful alongside which set produced it (#916).
    """
    from ..safety_commands import RULES_DIR, TOOLDEFS_DIR

    bundled_rules = Path(__file__).parent.parent / "hooks" / "damage-control" / "rules"
    bundled_tooldefs = Path(__file__).parent.parent / "tooldefs"

    if rules_dir is None:
        rules_dir = RULES_DIR if (RULES_DIR.exists() and any(RULES_DIR.glob("*.yaml"))) else bundled_rules
    if tooldefs_dir is None:
        tooldefs_dir = (
            TOOLDEFS_DIR if (TOOLDEFS_DIR.exists() and any(TOOLDEFS_DIR.glob("*.yaml")))
            else bundled_tooldefs
        )
    label = f"rules={rules_dir}, tooldefs={tooldefs_dir}"
    return load_config(rules_dir, tooldefs_dir), label


def render_report(report: PostureReport, label: str) -> list[str]:
    """Human-readable lines for the CLI. Empty list when there is nothing to say."""
    lines: list[str] = []
    if report.grant_errors:
        lines.append("unattended_allow entries that grant NOTHING as written:")
        lines.extend(f"  - {e}" for e in report.grant_errors)
    if report.unknown_grants:
        lines.append("unattended_allow names rule ids that do not exist in the loaded rules:")
        lines.extend(f"  - {rid}" for rid in report.unknown_grants)
        lines.append("  (a grant naming no rule is inert — check for tooldef/rule drift, #916)")
    if report.findings:
        exact = [f for f in report.findings if f.exact]
        fuzzy = [f for f in report.findings if not f.exact]
        lines.append(
            f"This task is specified to run {len(report.findings)} command(s) its "
            f"unattended posture will refuse:"
        )
        lines.extend(f"  - {f.render()}" for f in exact + fuzzy)
        if fuzzy:
            lines.append(
                "  (prompt findings are extracted from prose — treat as leads, not a complete list)"
            )
    if lines:
        lines.append(f"Measured against: {label}")
    return lines
