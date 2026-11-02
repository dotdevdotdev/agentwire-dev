#!/usr/bin/env python3
"""Canonical dump of the EFFECTIVE merged rule set (#1209 M2).

What the engine actually enforces is the merge of every installed rules file plus
the tooldefs, not any one file's bytes. Moving an entry between files must not
change that merge. This script prints it in a canonical, order-insensitive form
(sorted ids + patterns + tool policies + path lists; the ``source`` file label is
dropped because it is *where* an entry lives, not *what* it does, and ``reason`` is
prose shown to a human, not a decision).

    scripts/effective_rules.py                 # bundled rules in this tree
    scripts/effective_rules.py --ref <git>     # the bundled rules as of a git ref
    scripts/effective_rules.py --check         # exit 1 if differs from the golden
    scripts/effective_rules.py --write         # re-record corpus/golden/effective_rules.json

Re-recording is a deliberate behaviour change and belongs in its own PR.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "corpus" / "golden" / "effective_rules.json"
PACKAGE = "packages/damage-control-dev/src/damage_control"
# Two layouts, because `--ref` reads history: the package layout (#1209 M4) and the one before it.
LAYOUTS = (
    {"rules": (f"{PACKAGE}/rules", "agentwire/safety/rules"), "tooldefs": f"{PACKAGE}/tooldefs"},
    {"rules": ("agentwire/hooks/damage-control/rules", "agentwire/hooks/damage-control/agentwire-rules"),
     "tooldefs": "agentwire/tooldefs"},
)


def canonical(merged: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for key, items in merged.items():
        if key.startswith("_") or not isinstance(items, list):
            continue
        rows = []
        for it in items:
            if isinstance(it, dict):
                it = {k: v for k, v in it.items() if k not in ("source", "reason")}
            rows.append(json.dumps(it, sort_keys=True))
        out[key] = sorted(rows)
    return dict(sorted(out.items()))


def load(rules_dirs: List[Path], tooldefs: Path) -> Dict[str, List[str]]:
    sys.path.insert(0, str(REPO))
    from damage_control.engine import load_config

    with tempfile.TemporaryDirectory() as tmp:
        flat = Path(tmp)
        for d in rules_dirs:
            for f in d.glob("*.yaml"):
                (flat / f.name).write_bytes(f.read_bytes())
        merged = load_config(flat, tooldefs)
    bad = merged.get("_parse_failures") or merged.get("_duplicate_rule_ids")
    if bad:
        raise SystemExit(f"rule set does not load cleanly: {bad}")
    return canonical(merged)


def _load_root(root: Path) -> Dict[str, List[str]]:
    """Load whichever layout ``root`` holds (a dir that does not exist is simply absent)."""
    for layout in LAYOUTS:
        dirs = [root / d for d in layout["rules"] if (root / d).is_dir()]
        if dirs:
            return load(dirs, root / layout["tooldefs"])
    raise SystemExit(f"no rule directories found under {root}")


def _extract(ref: str, rel: str, dest: str) -> None:
    """Unpack ``rel`` as of ``ref`` into ``dest``; a path that does not exist at the ref is simply absent."""
    archive = subprocess.run(["git", "-C", str(REPO), "archive", ref, rel], capture_output=True)
    if archive.returncode == 0:
        subprocess.run(["tar", "-x", "-C", dest], input=archive.stdout, check=True)


def at_ref(ref: str) -> Dict[str, List[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        for layout in LAYOUTS:
            for rel in (*layout["rules"], layout["tooldefs"]):
                _extract(ref, rel, tmp)
        return _load_root(Path(tmp))


def current() -> Dict[str, List[str]]:
    return _load_root(REPO)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    data = at_ref(args.ref) if args.ref else current()
    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    if args.write:
        GOLDEN.write_text(text)
        print(f"wrote {GOLDEN.relative_to(REPO)}: { {k: len(v) for k, v in data.items()} }")
        return 0
    if args.check:
        if GOLDEN.read_text() != text:
            print("effective rule set differs from corpus/golden/effective_rules.json", file=sys.stderr)
            return 1
        print("effective rule set identical to golden")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
