#!/usr/bin/env python3
"""Generate ``agentwire/safety/rule_baselines.json`` — every version we ever shipped.

``heal_damage_control`` may not blind-overwrite ``~/.agentwire/damage-control/``
or ``~/.agentwire/tooldefs/``: those files are documented as host-editable, and
clobbering a hand-written rule is a worse failure than shipping a stale one. But
"install missing only" meant a rule file was written once at install and NEVER
updated again, so every rule fix this repo ships was inert on every existing
machine (#916).

The way out is a three-way comparison, and the missing leg is the common
ancestor. This script ships it: for each bundled rule/tooldef file, the sha256
of **every content it has ever had on the main line**, plus the current one. At
heal time:

    live bytes == bundled            -> nothing to do
    live sha256 in this manifest     -> a pristine OLDER RELEASE, safe to update
    live sha256 in neither           -> hand-edited, left alone and reported

Measured on the machine that reported #916, all 9 drifted rule files matched a
previously shipped hash, so the whole drift heals without touching a single
customization.

Usage:
    uv run python scripts/gen_rule_baselines.py           # write
    uv run python scripts/gen_rule_baselines.py --check   # exit 1 if stale

Run this whenever a bundled rule or tooldef YAML changes and commit both.
``tests/unit/test_rule_baselines.py`` fails loudly if you forget.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "agentwire" / "safety" / "rule_baselines.json"

# (manifest section, repo-relative dir)
SECTIONS = {
    "rules": Path("agentwire/hooks/damage-control/rules"),
    "tooldefs": Path("agentwire/tooldefs"),
}

HEADER = (
    "sha256 of every version of each bundled rule/tooldef file that has ever "
    "shipped on the main line. Used by heal_damage_control to tell a pristine "
    "older release (safe to update) from a hand-customized file (never "
    "touched). Regenerate with scripts/gen_rule_baselines.py (#916)."
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _historical_hashes(relpath: str) -> List[str]:
    """sha256 of every distinct blob this path has held across main-line history."""
    commits = _git("log", "--format=%H", "--follow", "--", relpath).split()
    blob_oids: List[str] = []
    seen_oids = set()
    for commit in commits:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", f"{commit}:{relpath}"],
            capture_output=True, text=True,
        )
        oid = proc.stdout.strip()
        if proc.returncode == 0 and oid and oid not in seen_oids:
            seen_oids.add(oid)
            blob_oids.append(oid)

    hashes = set()
    for oid in blob_oids:
        blob = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "cat-file", "blob", oid],
            capture_output=True, check=True,
        ).stdout
        hashes.add(hashlib.sha256(blob).hexdigest())
    return sorted(hashes)


def build_manifest() -> Dict[str, object]:
    manifest: Dict[str, object] = {"_comment": HEADER}
    for section, reldir in SECTIONS.items():
        entries: Dict[str, List[str]] = {}
        for src in sorted((REPO_ROOT / reldir).glob("*.yaml")):
            rel = str(reldir / src.name)
            hashes = set(_historical_hashes(rel))
            # The working-tree content always counts as shipped: this script is
            # run in the same commit that changes a rule, before that content
            # exists in history.
            hashes.add(hashlib.sha256(src.read_bytes()).hexdigest())
            entries[src.name] = sorted(hashes)
        manifest[section] = entries
    return manifest


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit 1 if the manifest is stale.")
    args = parser.parse_args(argv)

    manifest = build_manifest()
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    if args.check:
        current = MANIFEST_PATH.read_text() if MANIFEST_PATH.exists() else ""
        if current != rendered:
            print(
                "rule_baselines.json is stale. Run:\n"
                "  uv run python scripts/gen_rule_baselines.py",
                file=sys.stderr,
            )
            return 1
        print("rule_baselines.json up to date.")
        return 0

    MANIFEST_PATH.write_text(rendered)
    counts = {
        section: sum(len(v) for v in manifest[section].values())  # type: ignore[union-attr]
        for section in SECTIONS
    }
    print(f"Wrote {MANIFEST_PATH.relative_to(REPO_ROOT)} — {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
