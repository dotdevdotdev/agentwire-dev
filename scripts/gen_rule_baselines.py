#!/usr/bin/env python3
"""Generate ``agentwire/safety/rule_baselines.json`` — every version we ever shipped.

The pre-package ``heal`` could not blind-overwrite ``~/.agentwire/damage-control/``
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

PACKAGE = Path("packages/damage-control-dev/src/damage_control")

# (manifest section, repo-relative dir)
SECTIONS = {
    "rules": [
        PACKAGE / "rules",
        Path("agentwire/safety/rules"),
    ],
    "tooldefs": [PACKAGE / "tooldefs"],
}

# Where these files lived before the package (#1209 M4). `git log --follow` loses a rename
# that also edited the file, so every pre-move location is hashed explicitly: a pristine
# older install of a moved file must still read as "outdated", not "unknown".
LEGACY_DIRS = {
    "rules": [
        Path("agentwire/hooks/damage-control/rules"),
        Path("agentwire/hooks/damage-control/agentwire-rules"),
    ],
    "tooldefs": [Path("agentwire/tooldefs")],
}

# Files removed from the bundle. Their history stays in the manifest so doctor can
# tell a pristine leftover copy ("shipped once, yours now") from a hand-written
# file of the same name. Heal never deletes either (#1137).
RETIRED = {
    "rules": ["gws.yaml"],
    "tooldefs": ["gws.yaml"],
}

HEADER = (
    "sha256 of every version of each bundled rule/tooldef file that has ever "
    "shipped on the main line. Used once, by `agentwire safety migrate`, to tell a pristine "
    "older release (safe to delete) from a hand-customized file (kept). Regenerate with scripts/gen_rule_baselines.py (#916)."
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
    for section, reldirs in SECTIONS.items():
        entries: Dict[str, List[str]] = {}
        for reldir in reldirs:
            for src in sorted((REPO_ROOT / reldir).glob("*.yaml")):
                if src.name in entries:
                    raise SystemExit(f"{section}/{src.name} exists in two source dirs (the overlay is flat)")
                hashes = set(_historical_hashes(str(reldir / src.name)))
                for old in LEGACY_DIRS[section]:
                    hashes.update(_historical_hashes(str(old / src.name)))
                # The working-tree content always counts as shipped: this script is
                # run in the same commit that changes a rule, before that content
                # exists in history.
                hashes.add(hashlib.sha256(src.read_bytes()).hexdigest())
                entries[src.name] = sorted(hashes)
        for name in RETIRED.get(section, []):
            if name not in entries:
                entries[name] = sorted({h for d in LEGACY_DIRS[section] for h in _historical_hashes(str(d / name))})
        manifest[section] = dict(sorted(entries.items()))
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
