#!/usr/bin/env python3
"""Mutation check for the #914 scoped-grant tests.

A test that never failed is indistinguishable from one whose anchor never
matched — six bugs shipped past a fully green suite here on 2026-08-05 for
exactly that reason. So each safety property below is broken deliberately, one
at a time, and the run records WHICH tests went red.

Each mutation is a plausible weaker implementation, not a syntax error: the
point is to prove the test discriminates between the correct check and the
obvious wrong one, not that it notices a crash.

Usage:
    uv run --extra dev python scripts/mutate_scoped_allow.py          # run all
    uv run --extra dev python scripts/mutate_scoped_allow.py --list   # names only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "agentwire" / "safety" / "_core.py"
TESTS = "tests/unit/test_unattended_scoped_allow.py"


@dataclass
class Mutation:
    name: str
    property_broken: str
    old: str
    new: str


MUTATIONS = [
    Mutation(
        name="drop-candidate-realpath",
        property_broken="a symlink out of the scope dir is caught",
        old="    if real != normalized:\n        out.append(real)\n    return out",
        new="    return out",
    ),
    Mutation(
        name="scope-is-a-string-prefix",
        property_broken="a sibling sharing the scope's prefix is out of scope",
        old='            if re.fullmatch(regex + r"(?:/.*)?", normalized):\n                return True',
        new="            if normalized.startswith(variant):\n                return True",
    ),
    Mutation(
        name="only-first-git-selector",
        property_broken="every repo selector is read (--git-dir AND --work-tree)",
        old='        for selector in (gopts["git_dir"], gopts["work_tree"]):',
        new='        for selector in (gopts["git_dir"],):',
    ),
    Mutation(
        name="cd-trusted-regardless-of-operator",
        property_broken="`cd X ; cmd` is unresolvable (the cd may have failed)",
        old='            if following_op != "&&":',
        new="            if False:",
    ),
    Mutation(
        name="any-path-in-scope-is-enough",
        property_broken="ALL target dirs must be in scope, not merely one",
        old="    outside = [d for d in dirs if not any(path_in_scope(d, p) for p in flat)]",
        new="    outside = [] if any(\n        path_in_scope(d, p) for d in dirs for p in flat\n    ) else list(dirs)",
    ),
    Mutation(
        name="grants-union-instead-of-precedence",
        property_broken="a scoped task entry replaces the unscoped default",
        old="            if rid not in merged:\n                merged[rid] = scopes",
        new="            merged.setdefault(rid, []).extend(scopes)",
    ),
    Mutation(
        name="wire-format-flattens-scopes",
        property_broken="a spawned child inherits the SCOPE, not a bare grant",
        old='    if any(not isinstance(e, str) for e in items):\n        return json.dumps(items, separators=(",", ":"))',
        new='    items = [e if isinstance(e, str) else str(e.get("id", "")) for e in items]',
    ),
    Mutation(
        name="no-scope-root-resolution",
        property_broken="a scope rooted behind a symlink still admits its contents",
        old="    if real != literal:\n        variants.append(real + expanded[len(literal):])",
        new="    if False:\n        variants.append(real + expanded[len(literal):])",
    ),
    Mutation(
        name="unscopeable-falls-back-to-cwd",
        property_broken="an unresolvable target refuses instead of guessing cwd",
        old='        return [], f"command runs through {head} — target directory is not statically knowable"',
        new="        dirs.extend(_resolve_dir('.', current_dir)); continue",
    ),
    Mutation(
        name="dash-c-resolved-against-cwd",
        property_broken="the -C chain is CUMULATIVE, each hop against the previous result",
        old='        acting_dir = current_dir\n        for hop in gopts["chdir"]:\n            acting_dir = _abs_path(hop, acting_dir)',
        new='        acting_dir = current_dir\n        for hop in gopts["chdir"]:\n            acting_dir = _abs_path(hop, current_dir)',
    ),
    Mutation(
        name="git-dir-resolved-against-cwd",
        property_broken="--git-dir/--work-tree resolve against the -C result, not the cwd",
        old='            if selector:\n                dirs.extend(_resolve_dir(selector, acting_dir))',
        new='            if selector:\n                dirs.extend(_resolve_dir(selector, current_dir))',
    ),
    Mutation(
        name="env-assignments-discarded",
        property_broken="GIT_DIR=<other> git commit is a repo redirect, not noise",
        old="        for assign in assigns:\n            name, _, value = assign.partition(\"=\")",
        new="        for assign in []:\n            name, _, value = assign.partition(\"=\")",
    ),
    Mutation(
        name="no-repo-root-walk-up",
        property_broken="a scope over a repo SUBDIRECTORY does not grant over the repo",
        old='        if head == "git":',
        new="        if False:",
    ),
    Mutation(
        name="malformed-entry-falls-through",
        property_broken="a refused entry BINDS its rule instead of falling back to a looser layer",
        old="        grants.setdefault(rid, [])\n        unknown =",
        new="        unknown =",
    ),
    Mutation(
        name="mcp-scope-measured-against-cwd",
        property_broken="a scoped grant refuses when there is no filesystem target",
        old="    if not scopeable:\n        return False, (",
        new="    if False:\n        return False, (",
    ),
]


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        ["uv", "run", "--extra", "dev", "pytest", TESTS, "-q", "--no-header", "-x"],
        cwd=REPO, capture_output=True, text=True, timeout=900,
    )
    return proc.returncode, proc.stdout + proc.stderr


def failing_tests(output: str) -> list[str]:
    return sorted({
        line.split(" ")[1] if line.startswith("FAILED ") else line
        for line in output.splitlines()
        if line.startswith("FAILED ")
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list mutation names and exit")
    args = parser.parse_args()

    if args.list:
        for m in MUTATIONS:
            print(f"{m.name}: {m.property_broken}")
        return 0

    original = CORE.read_text()
    code, output = run_tests()
    if code != 0:
        print("BASELINE IS RED — fix the suite before mutating.\n" + output[-3000:])
        return 1
    print(f"baseline: GREEN ({TESTS})\n")

    survivors = []
    try:
        for m in MUTATIONS:
            if m.old not in original:
                print(f"[SKIP] {m.name}: anchor text not found — mutation is stale")
                survivors.append(m.name)
                continue
            CORE.write_text(original.replace(m.old, m.new, 1))
            code, output = run_tests()
            caught = code != 0
            marker = "RED  " if caught else "GREEN"
            print(f"[{marker}] {m.name}")
            print(f"        property: {m.property_broken}")
            if caught:
                for t in failing_tests(output)[:4]:
                    print(f"        caught by: {t}")
            else:
                print("        *** SURVIVED — no test discriminates this ***")
                survivors.append(m.name)
    finally:
        CORE.write_text(original)

    code, _ = run_tests()
    print(f"\nrestored baseline: {'GREEN' if code == 0 else 'RED'}")
    if survivors:
        print(f"SURVIVING MUTATIONS ({len(survivors)}): {', '.join(survivors)}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
