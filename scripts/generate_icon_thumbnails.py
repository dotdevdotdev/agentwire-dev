#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["Pillow>=10"]
# ///
"""Pre-generate small WebP thumbnails for portal icons.

The portal renders icons as ~48px sidebar avatars but the source files are
1024px+ PNG/JPEG (the full icons dir is ~19MB). This script writes a
``<stem>.webp`` thumbnail next to every source icon so the portal can serve
the small variants instead of the full-res originals.

Re-run after adding or replacing source icons, then commit the ``.webp`` files.

    ./scripts/generate_icon_thumbnails.py            # generate missing/stale
    ./scripts/generate_icon_thumbnails.py --force    # regenerate all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

ICONS_DIR = Path(__file__).resolve().parent.parent / "agentwire" / "static" / "icons"
SOURCE_SUFFIXES = {".png", ".jpeg", ".jpg"}
MAX_DIM = 128  # longest edge in px; sidebar avatars render at 48px
QUALITY = 80


def iter_sources(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES:
            yield path


def needs_rebuild(src: Path, dst: Path, force: bool) -> bool:
    if force or not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime


def make_thumbnail(src: Path, dst: Path) -> int:
    with Image.open(src) as img:
        img = img.convert("RGBA") if img.mode in ("P", "LA") else img.convert("RGB")
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
        img.save(dst, "WEBP", quality=QUALITY, method=6)
    return dst.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="regenerate all thumbnails")
    args = parser.parse_args()

    if not ICONS_DIR.exists():
        print(f"icons dir not found: {ICONS_DIR}", file=sys.stderr)
        return 1

    built = 0
    src_total = 0
    dst_total = 0
    for src in iter_sources(ICONS_DIR):
        dst = src.with_suffix(".webp")
        src_total += src.stat().st_size
        if needs_rebuild(src, dst, args.force):
            size = make_thumbnail(src, dst)
            built += 1
            print(f"  {src.relative_to(ICONS_DIR)} -> {dst.name} ({size:,} B)")
        dst_total += dst.stat().st_size

    print(
        f"\n{built} thumbnail(s) built. "
        f"Sources: {src_total / 1e6:.1f}MB  Thumbnails: {dst_total / 1e6:.2f}MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
