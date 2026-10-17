#!/usr/bin/env python3
"""Expand ``pattern_src:`` into the shipped ``pattern:`` of the bundled rule YAMLs (#1125).

Destructive-flag rules must see a flag after ANY operand (``rm foo -rf x``), so
they are authored with an ``{ARGS}`` placeholder ("any leading arguments of this
command segment") under ``pattern_src:``. The placeholder is expanded HERE, at
build time, into the plain ``pattern:`` the hook reads. The hook never expands
anything, so a hook and a rule set from different releases can never disagree:
an old hook reading new rules sees an ordinary valid regex, not a literal
``{ARGS}`` that matches nothing (which failed fully open on the first cut).

A token is an argument unless it is a segment separator (``; & |`` — flags past
those belong to the next command) or the bare ``--`` that ends option parsing
(everything after it is an operand, so a file named ``-rf`` stays allowed).
Quoted operands are already masked in the anchored haystack.

Usage:
    uv run python scripts/gen_rule_patterns.py           # write
    uv run python scripts/gen_rule_patterns.py --check   # exit 1 if stale

Layout contract: ``pattern_src:`` sits on the line directly after its ``pattern:``.
"""

import argparse
import re
import sys
from pathlib import Path

RULES_DIR = Path(__file__).resolve().parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"

ARGS_FRAGMENT = r"(?:(?!--(?:\s|$))[^\s;&|]+\s+)*?"

_PATTERN_LINE = re.compile(r"^(\s*(?:- )?pattern: )'(.*)'$")
_SRC_LINE = re.compile(r"^(\s*)pattern_src: '(.*)'$")


def expand(src: str) -> str:
    return src.replace("{ARGS}", ARGS_FRAGMENT)


def regenerate(text: str) -> str:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        src = _SRC_LINE.match(line)
        if not src:
            continue
        head = _PATTERN_LINE.match(lines[i - 1]) if i else None
        if not head:
            raise SystemExit(f"pattern_src without a pattern: line above it (line {i + 1})")
        lines[i - 1] = f"{head.group(1)}'{expand(src.group(2))}'"
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit 1 if a shipped pattern is stale.")
    args = parser.parse_args()
    stale = []
    for path in sorted(RULES_DIR.glob("*.yaml")):
        text = path.read_text()
        new = regenerate(text)
        if new != text:
            stale.append(path.name)
            if not args.check:
                path.write_text(new)
    if args.check and stale:
        print(f"stale: {', '.join(stale)} — run scripts/gen_rule_patterns.py", file=sys.stderr)
        return 1
    print("rule patterns up to date." if args.check else f"wrote: {', '.join(stale) or 'nothing to change'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
