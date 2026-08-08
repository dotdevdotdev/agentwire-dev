#!/usr/bin/env python3
"""Regenerate damage-control hook scripts by inlining ``agentwire/safety/_core.py``.

The Claude Code hooks (bash/edit/write) must run as PEP 723 standalone scripts
— they can't ``from agentwire.safety._core import ...`` because uv runs them
in an isolated env with only ``pyyaml`` as a dep. So we inline ``_core.py``
content into each hook between::

    # === BEGIN GENERATED FROM agentwire/safety/_core.py ===
    ...
    # === END GENERATED ===

This script reads ``_core.py``, finds the marker block in each hook, and
replaces the contents between the markers. Idempotent — re-running on
already-synced hooks is a no-op.

Each hook also carries a **stamp** just above the generated block::

    AGENTWIRE_HOOK_STAMP = {"core_sha256": "…", "generated_at": "…Z"}

The stamp exists so the deployed copy can be ORDERED against the package,
not merely compared to it (#936). ``heal_damage_control`` used to overwrite a
hook whenever the bytes DIFFERED, in either direction, and ``shutil.copy2``
preserved the source mtime — so a checkout predating a security fix could
reinstall the old hook machine-wide and leave nothing, not even a timestamp,
to show it had happened. With the stamp, ``agentwire doctor`` can say "the
installed hook is older than the package" (and the reverse), which it
structurally could not before.

``generated_at`` only moves when the inlined ``_core.py`` actually changes, so
this script and its ``--check`` mode stay idempotent.

Usage:
    uv run python scripts/regen_damage_control_hooks.py        # write
    uv run python scripts/regen_damage_control_hooks.py --check  # exit 1 if drift

When ``_core.py`` changes, run this and commit both files together. CI runs
the sync test (``tests/unit/test_damage_control_sync.py``) which will fail
loudly if the hooks fall out of date.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_PATH = REPO_ROOT / "agentwire" / "safety" / "_core.py"
HOOKS_DIR = REPO_ROOT / "agentwire" / "hooks" / "damage-control"

HOOK_FILES = [
    "bash-tool-damage-control.py",
    "edit-tool-damage-control.py",
    "write-tool-damage-control.py",
    "read-tool-damage-control.py",
    "mcp-tool-damage-control.py",
]

BEGIN_MARKER = "# === BEGIN GENERATED FROM agentwire/safety/_core.py ==="
END_MARKER = "# === END GENERATED ==="

STAMP_BEGIN = "# === BEGIN AGENTWIRE HOOK STAMP (generated — do not edit) ==="
STAMP_END = "# === END AGENTWIRE HOOK STAMP ==="
STAMP_VAR = "AGENTWIRE_HOOK_STAMP"


def core_sha256(core_text: str) -> str:
    return hashlib.sha256(core_text.encode()).hexdigest()


def read_stamp(hook_text: str) -> Optional[dict]:
    """Parse the stamp block out of a hook script; None if it carries none."""
    if STAMP_BEGIN not in hook_text or STAMP_END not in hook_text:
        return None
    _, _, rest = hook_text.partition(STAMP_BEGIN)
    body, _, _ = rest.partition(STAMP_END)
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(f"{STAMP_VAR} = "):
            try:
                return json.loads(line.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                return None
    return None


def _stamp_block(sha: str, generated_at: str) -> str:
    payload = json.dumps({"core_sha256": sha, "generated_at": generated_at}, sort_keys=True)
    return f"{STAMP_BEGIN}\n{STAMP_VAR} = {payload}\n{STAMP_END}\n"


def _strip_stamp(hook_text: str) -> str:
    if STAMP_BEGIN not in hook_text or STAMP_END not in hook_text:
        return hook_text
    head, _, rest = hook_text.partition(STAMP_BEGIN)
    _, _, tail = rest.partition(STAMP_END)
    return head + tail.lstrip("\n")


def _render_hook(hook_text: str, core_text: str, generated_at: str) -> str:
    """Replace the marker block in ``hook_text`` with ``core_text`` + restamp."""
    if BEGIN_MARKER not in hook_text or END_MARKER not in hook_text:
        raise SystemExit(
            f"Hook is missing marker block. Expected:\n  {BEGIN_MARKER}\n  ...\n  {END_MARKER}"
        )
    stamped = _strip_stamp(hook_text)
    head, _, rest = stamped.partition(BEGIN_MARKER)
    _, _, tail = rest.partition(END_MARKER)
    stamp = _stamp_block(core_sha256(core_text), generated_at)
    return f"{head}{stamp}{BEGIN_MARKER}\n{core_text.strip()}\n{END_MARKER}{tail}"


def _is_current(hook_text: str, core_text: str) -> bool:
    """True when the hook inlines this core AND carries the matching stamp."""
    stamp = read_stamp(hook_text)
    if not stamp or stamp.get("core_sha256") != core_sha256(core_text):
        return False
    if BEGIN_MARKER not in hook_text or END_MARKER not in hook_text:
        return False
    _, _, rest = hook_text.partition(BEGIN_MARKER)
    inlined, _, _ = rest.partition(END_MARKER)
    return inlined.strip() == core_text.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if any hook would change instead of writing.",
    )
    args = parser.parse_args(argv)

    if not CORE_PATH.exists():
        print(f"error: {CORE_PATH} not found", file=sys.stderr)
        return 1

    core_text = CORE_PATH.read_text()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    drift_found = False
    wrote_any = False

    for hook_name in HOOK_FILES:
        hook_path = HOOKS_DIR / hook_name
        if not hook_path.exists():
            print(f"error: {hook_path} not found", file=sys.stderr)
            return 1
        current = hook_path.read_text()
        # Only restamp when the inlined core actually changed — otherwise
        # `generated_at` would move on every run and --check could never pass.
        if _is_current(current, core_text):
            print(f"  {hook_name}: up to date")
            continue
        drift_found = True
        if args.check:
            print(f"  {hook_name}: OUT OF SYNC", file=sys.stderr)
        else:
            hook_path.write_text(_render_hook(current, core_text, generated_at))
            print(f"  {hook_name}: regenerated")
            wrote_any = True

    if args.check and drift_found:
        print(
            "\nDrift detected. Run:  uv run python scripts/regen_damage_control_hooks.py",
            file=sys.stderr,
        )
        return 1

    if not args.check and not wrote_any:
        print("All hooks already in sync.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
