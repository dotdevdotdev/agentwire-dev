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

Usage:
    uv run python scripts/regen_damage_control_hooks.py        # write
    uv run python scripts/regen_damage_control_hooks.py --check  # exit 1 if drift

When ``_core.py`` changes, run this and commit both files together. CI runs
the sync test (``tests/unit/test_damage_control_sync.py``) which will fail
loudly if the hooks fall out of date.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_PATH = REPO_ROOT / "agentwire" / "safety" / "_core.py"
HOOKS_DIR = REPO_ROOT / "agentwire" / "hooks" / "damage-control"

HOOK_FILES = [
    "bash-tool-damage-control.py",
    "edit-tool-damage-control.py",
    "write-tool-damage-control.py",
]

BEGIN_MARKER = "# === BEGIN GENERATED FROM agentwire/safety/_core.py ==="
END_MARKER = "# === END GENERATED ==="


def _render_hook(hook_text: str, core_text: str) -> str:
    """Replace the marker block in ``hook_text`` with ``core_text``."""
    if BEGIN_MARKER not in hook_text or END_MARKER not in hook_text:
        raise SystemExit(
            f"Hook is missing marker block. Expected:\n  {BEGIN_MARKER}\n  ...\n  {END_MARKER}"
        )
    head, _, rest = hook_text.partition(BEGIN_MARKER)
    _, _, tail = rest.partition(END_MARKER)
    return f"{head}{BEGIN_MARKER}\n{core_text.strip()}\n{END_MARKER}{tail}"


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
    drift_found = False
    wrote_any = False

    for hook_name in HOOK_FILES:
        hook_path = HOOKS_DIR / hook_name
        if not hook_path.exists():
            print(f"error: {hook_path} not found", file=sys.stderr)
            return 1
        current = hook_path.read_text()
        rendered = _render_hook(current, core_text)
        if rendered == current:
            print(f"  {hook_name}: up to date")
            continue
        drift_found = True
        if args.check:
            print(f"  {hook_name}: OUT OF SYNC", file=sys.stderr)
        else:
            hook_path.write_text(rendered)
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
