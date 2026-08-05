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
    findings: list[Finding] = []
    for dep in _dependencies(payload):
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


def _dependencies(payload) -> list[dict]:
    deps = payload.get("dependencies") if isinstance(payload, dict) else payload
    return [d for d in (deps or []) if isinstance(d, dict)]


def audited_count(payload) -> int:
    """How many packages the audit actually LOOKED AT.

    Zero findings is only good news if something was examined. Without this,
    "124 packages, none vulnerable" and "nothing was audited at all" produce
    byte-identical output — an empty ``requirements.txt`` (a silently-failed
    export, a bad ``--no-dev`` interaction) makes pip-audit exit 0 with
    ``{"dependencies": []}``, and a report keyed only on findings calls that
    clean.

    Which is the same conflation this whole script exists to prevent, one level
    in: #900 was "a red audit renders as green", and counting only findings
    would let "audited nothing" render as green too.
    """
    return len(_dependencies(payload))


def implausible(audited: int, minimum: int) -> str:
    """Why this audit's *coverage* is untrustworthy, or '' if it looks sane."""
    if audited == 0:
        return "the audit examined ZERO packages"
    if minimum and audited < minimum:
        return f"the audit examined only {audited} packages (expected >= {minimum})"
    return ""


def annotations(findings: list[Finding], *, scope: str, audited: int = 0,
                minimum: int = 0) -> list[str]:
    """GitHub workflow-command lines. Rendered on the run and PR checks views."""
    doubt = implausible(audited, minimum)
    lines = []
    if doubt:
        lines.append(
            f"::warning title=pip-audit ({scope}): coverage UNKNOWN::{doubt} — "
            "so 'no advisories' here means nothing was looked at, not that "
            "nothing is wrong. Check the export step."
        )
    if not findings:
        if not doubt:
            lines.append(f"::notice::pip-audit ({scope}): "
                         f"no known advisories across {audited} packages")
        return lines
    # Findings are reported even when coverage is doubted. Suppressing them
    # behind the coverage warning would be the worst of both: an incomplete
    # audit that ALSO hides what it did manage to find.
    lines.append(
        f"::warning title=pip-audit ({scope}): "
        f"{len(findings)} advisor{'y' if len(findings) == 1 else 'ies'}::"
        f"{summary_line(findings, audited=audited)}"
    )
    for f in findings:
        lines.append(
            f"::warning title={f.package} {f.version} — {f.id}::"
            f"fix: {f.fix}"
        )
    return lines


def summary_line(findings: list[Finding], *, audited: int = 0) -> str:
    if not findings:
        return f"no known advisories across {audited} packages"
    fixable = [f for f in findings if f.fixable]
    packages = sorted({f.package for f in findings})
    part = (f"{len(findings)} advisor{'y' if len(findings) == 1 else 'ies'} in "
            f"{len(packages)} of {audited} package(s): {', '.join(packages)}")
    if fixable:
        part += f" — {len(fixable)} have published fixes (lock refresh)"
    return part


def markdown(findings: list[Finding], *, scope: str, audited: int = 0,
             minimum: int = 0) -> str:
    """The step-summary body. Counts are in the heading, on purpose."""
    doubt = implausible(audited, minimum)
    if doubt and not findings:
        return (
            f"## pip-audit ({scope}): coverage UNKNOWN\n\n"
            f"**{doubt}.**\n\n"
            "Zero advisories over zero packages is not a clean bill of health — "
            "it means the dependency export produced nothing to audit. Check the "
            "`uv export` step before trusting this run.\n"
        )
    doubt_note = (
        f"\n> **Coverage is also suspect:** {doubt}. The findings below are real, "
        "but the set they were found in is incomplete — there may be more.\n"
        if doubt else ""
    )
    if not findings:
        return (f"## pip-audit ({scope}): clean\n\n"
                f"No known advisories across **{audited} packages**.\n")
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
        f"{'y' if len(findings) == 1 else 'ies'} across {audited} packages\n\n"
        "Advisory only — this does not block the merge. It is reported here so a red "
        "audit cannot render as a green workflow (#900).\n"
        f"{doubt_note}\n"
        "| Package | Version | Advisory | Fix versions |\n"
        "|---|---|---|---|\n"
        f"{rows}\n{note}"
    )


def issue_body(findings: list[Finding], *, scope: str, run_url: str = "",
               audited: int = 0, minimum: int = 0) -> str:
    link = f"\n\n[Latest audit run]({run_url})" if run_url else ""
    return (
        markdown(findings, scope=scope, audited=audited, minimum=minimum)
        + "\n_Filed and updated automatically by the weekly `security` workflow, "
        "which reopens this issue if the findings come back and closes it when "
        "they are gone._" + link + "\n"
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
    ap.add_argument("--min-packages", type=int, default=0,
                    help="a plausible floor on packages audited; below it, "
                         "coverage is reported as UNKNOWN rather than clean")
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
    audited = audited_count(payload)
    kw = {"scope": args.scope, "audited": audited, "minimum": args.min_packages}

    for line in annotations(findings, **kw):
        print(line)
    _write("GITHUB_STEP_SUMMARY", markdown(findings, **kw))
    _write("GITHUB_OUTPUT", f"count={len(findings)}")
    _write("GITHUB_OUTPUT", f"audited={audited}")
    _write("GITHUB_OUTPUT", f"summary={summary_line(findings, audited=audited)}")

    if args.issue_body:
        args.issue_body.write_text(issue_body(run_url=args.run_url, findings=findings, **kw))

    return 1 if (args.exit_code and findings) else 0


if __name__ == "__main__":
    sys.exit(main())
