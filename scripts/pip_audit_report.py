#!/usr/bin/env python3
"""Render pip-audit JSON so a RED audit cannot render as GREEN (#900).

The pip-audit job is deliberately ``continue-on-error: true``: hard-blocking
every PR on transitive CVEs is noise, and the damage-control bypass corpus is
the real merge gate. That decision stands. Its side effect did not — a failing
audit became indistinguishable from a passing one at the workflow level, so
nobody saw it unless they opened the job, and eight advisories accumulated
before a PR's check list happened to be in front of someone.

Non-blocking and impossible to miss pull against each other, so this splits the
difference across three surfaces rather than picking one:

1. **Annotations** (``::warning::``) — GitHub renders these on the run page and
   in the PR's checks view WITHOUT anyone opening the job log. Immediate,
   zero conversation noise.
2. **Step summary** — a table on the run page, with the count in the heading,
   so "is it clean?" is answerable at a glance.
3. **A single tracking issue**, opened/updated by the weekly cron. This is the
   one that actually stops accumulation: annotations and summaries belong to a
   run nobody revisits, whereas an issue is this repo's own SSOT for work, it
   dedupes to one, and it survives until someone closes it.

Deliberately NOT a per-PR comment. Most findings are not caused by the PR they
appear on (#900's eight were live on main and surfaced on a PR that changed
zero dependency inputs), so commenting on unrelated PRs trains people to
scroll past exactly the signal this is trying to preserve.

Exit code is 0 unless ``--exit-code`` is passed, so wiring this in can never
turn the advisory job into a gate by accident.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ISSUE_TITLE = "pip-audit: unresolved advisories in the dependency lock"
ISSUE_LABEL = "area:security"


@dataclass
class Finding:
    package: str
    version: str
    id: str
    fix_versions: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @property
    def fix(self) -> str:
        return ", ".join(self.fix_versions) if self.fix_versions else "none published"

    @property
    def fixable(self) -> bool:
        """A fix exists upstream — i.e. this is a lock refresh, not a dead end.

        The distinction that mattered in #900: seven of the eight findings were
        fixed by versions our own bounds ALREADY allowed, so they were a stale
        lockfile rather than a security decision anyone had made.
        """
        return bool(self.fix_versions)


def parse(payload: dict) -> list[Finding]:
    """Findings from pip-audit's ``--format json`` output.

    Tolerates both the ``{"dependencies": [...]}`` envelope and a bare list —
    pip-audit has shipped both, and a report tool that raises on the shape it
    was handed is worse than useless on the day it is needed.
    """
    deps = payload.get("dependencies") if isinstance(payload, dict) else payload
    findings: list[Finding] = []
    for dep in deps or []:
        if not isinstance(dep, dict):
            continue
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, dict):
                continue
            findings.append(Finding(
                package=dep.get("name", "?"),
                version=dep.get("version", "?"),
                id=vuln.get("id", "?"),
                fix_versions=list(vuln.get("fix_versions") or []),
                aliases=list(vuln.get("aliases") or []),
            ))
    return sorted(findings, key=lambda f: (f.package, f.id))


def annotations(findings: list[Finding], *, scope: str) -> list[str]:
    """GitHub workflow-command lines. Rendered on the run and PR checks views."""
    if not findings:
        return [f"::notice::pip-audit ({scope}): no known advisories"]
    lines = [
        f"::warning title=pip-audit ({scope}): "
        f"{len(findings)} advisor{'y' if len(findings) == 1 else 'ies'}::"
        f"{summary_line(findings)}"
    ]
    for f in findings:
        lines.append(
            f"::warning title={f.package} {f.version} — {f.id}::"
            f"fix: {f.fix}"
        )
    return lines


def summary_line(findings: list[Finding]) -> str:
    if not findings:
        return "no known advisories"
    fixable = [f for f in findings if f.fixable]
    packages = sorted({f.package for f in findings})
    part = f"{len(findings)} advisor{'y' if len(findings) == 1 else 'ies'} in {len(packages)} package(s): {', '.join(packages)}"
    if fixable:
        part += f" — {len(fixable)} have published fixes (lock refresh)"
    return part


def markdown(findings: list[Finding], *, scope: str) -> str:
    """The step-summary body. The count is in the heading, on purpose."""
    if not findings:
        return f"## pip-audit ({scope}): clean\n\nNo known advisories in the audited set.\n"
    rows = "\n".join(
        f"| `{f.package}` | {f.version} | {f.id} | {f.fix} |" for f in findings
    )
    fixable = sum(1 for f in findings if f.fixable)
    note = (
        f"\n**{fixable} of {len(findings)} have a published fix.** If the fix version is "
        "inside the bound already declared in `pyproject.toml`, this is a stale lock — "
        "`uv lock --upgrade-package <name>`, not a `pyproject` change.\n"
        if fixable else ""
    )
    return (
        f"## pip-audit ({scope}): {len(findings)} advisor"
        f"{'y' if len(findings) == 1 else 'ies'}\n\n"
        "Advisory only — this does not block the merge. It is reported here so a red "
        "audit cannot render as a green workflow (#900).\n\n"
        "| Package | Version | Advisory | Fix versions |\n"
        "|---|---|---|---|\n"
        f"{rows}\n{note}"
    )


def issue_body(findings: list[Finding], *, scope: str, run_url: str = "") -> str:
    link = f"\n\n[Latest audit run]({run_url})" if run_url else ""
    return (
        markdown(findings, scope=scope)
        + "\n_Filed and updated automatically by the weekly `security` workflow. "
        "Close it once the lock is refreshed; it reopens itself if the findings "
        "come back._" + link + "\n"
    )


def _write(path_env: str, text: str) -> None:
    """Append to a GitHub file-command path, if we are running under Actions."""
    target = os.environ.get(path_env)
    if not target:
        return
    with open(target, "a") as fh:
        fh.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("report", type=Path, help="pip-audit --format json output")
    ap.add_argument("--scope", default="runtime", help="which dependency set was audited")
    ap.add_argument("--issue-body", type=Path,
                    help="also write a tracking-issue body here")
    ap.add_argument("--run-url", default=os.environ.get("AUDIT_RUN_URL", ""))
    ap.add_argument("--exit-code", action="store_true",
                    help="exit 1 when findings exist (NOT used by the advisory job)")
    args = ap.parse_args(argv)

    try:
        payload = json.loads(args.report.read_text())
    except (OSError, ValueError) as exc:
        # A missing or unparseable report is itself a finding: it means the
        # audit did not run, which is the exact failure mode #900 is about.
        print(f"::warning title=pip-audit ({args.scope}): report unreadable::{exc}")
        _write("GITHUB_STEP_SUMMARY",
               f"## pip-audit ({args.scope}): report unreadable\n\n`{exc}`\n\n"
               "The audit did not produce a report, so its result is UNKNOWN — "
               "which is not the same as clean.")
        return 1 if args.exit_code else 0

    findings = parse(payload)
    for line in annotations(findings, scope=args.scope):
        print(line)
    _write("GITHUB_STEP_SUMMARY", markdown(findings, scope=args.scope))
    _write("GITHUB_OUTPUT", f"count={len(findings)}")
    _write("GITHUB_OUTPUT", f"summary={summary_line(findings)}")

    if args.issue_body:
        args.issue_body.write_text(
            issue_body(findings, scope=args.scope, run_url=args.run_url))

    return 1 if (args.exit_code and findings) else 0


if __name__ == "__main__":
    sys.exit(main())
