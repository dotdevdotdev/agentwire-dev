#!/usr/bin/env python3
"""Canonical dump of the EFFECTIVE merged rule set (#1209 M2).

What the engine actually enforces is the merge of every installed rules file plus
the tooldefs, not any one file's bytes. Moving an entry between files must not
change that merge. This script prints it in a canonical, order-insensitive form
(sorted ids + patterns + tool policies + path lists; the ``source`` file label is
dropped because it is *where* an entry lives, not *what* it does, and ``reason`` is
prose shown to a human, not a decision).

    scripts/effective_rules.py                 # the INSTALLED engine's rules + agentwire/safety/rules
    scripts/effective_rules.py --check         # exit 1 if differs from the golden
    scripts/effective_rules.py --write         # re-record corpus/golden/effective_rules.json

Re-recording is a deliberate behaviour change and belongs in its own PR.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "corpus" / "golden" / "effective_rules.json"
RULES_LAYER = REPO / "agentwire" / "safety" / "rules"


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


def current() -> Dict[str, List[str]]:
    """The installed engine's bundled rules + tooldefs, merged with AgentWire's rules layer."""
    from damage_control.engine import bundled_rules_dir, bundled_tooldefs_dir, load_config

    with tempfile.TemporaryDirectory() as tmp:
        flat = Path(tmp)
        for d in (bundled_rules_dir(), RULES_LAYER):
            for f in d.glob("*.yaml"):
                (flat / f.name).write_bytes(f.read_bytes())
        merged = load_config(flat, bundled_tooldefs_dir())
    bad = merged.get("_parse_failures") or merged.get("_duplicate_rule_ids")
    if bad:
        raise SystemExit(f"rule set does not load cleanly: {bad}")
    return canonical(merged)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    data = current()
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
