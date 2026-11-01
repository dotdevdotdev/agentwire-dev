#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""Build the per-file JSON form of the bundled rule and tooldef YAML sources.

YAML stays the authored form; the hook reads the JSON, so a hook process never imports
PyYAML (and never pays ~45 ms to parse 26 files) just to learn its own rules. The JSON is
committed next to its source so an editable checkout, an sdist and a wheel all carry it with
no build hook, and ``--check`` (run by the test suite) fails loudly if the two drift.

    build_rules_json.py DIR [DIR ...]           # write DIR/<stem>.json for every DIR/<stem>.yaml
    build_rules_json.py --check DIR [DIR ...]   # exit 1 if any JSON is missing, stale or orphaned
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List


def render(yaml_path: Path) -> str:
    import yaml

    data: Any = yaml.safe_load(yaml_path.read_text()) or {}
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    if json.loads(text) != data:
        raise SystemExit(f"{yaml_path}: does not round-trip through JSON")
    return text


def sync(directories: List[Path], check: bool) -> List[str]:
    """Write (or, with ``check``, only compare) the JSON; return the problems found."""
    problems: List[str] = []
    for directory in directories:
        sources = sorted(directory.glob("*.yaml"))
        if not sources:
            problems.append(f"{directory}: no *.yaml sources")
        for source in sources:
            target = source.with_suffix(".json")
            want = render(source)
            have = target.read_text() if target.exists() else None
            if have == want:
                continue
            problems.append(f"{target}: {'missing' if have is None else 'out of date'}")
            if not check:
                target.write_text(want)
        orphans = {p.stem for p in directory.glob("*.json")} - {p.stem for p in sources}
        problems += [f"{directory / (stem + '.json')}: no YAML source" for stem in sorted(orphans)]
    return problems


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dirs", nargs="+", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    problems = sync(args.dirs, args.check)
    for problem in problems:
        print(problem, file=sys.stderr if args.check else sys.stdout)
    if args.check and problems:
        print("run: packages/damage-control-dev/tools/build_rules_json.py <dirs>", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
