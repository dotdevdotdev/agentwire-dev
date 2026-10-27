"""Behavioural guard for the Session HUD drawer height after expanding cards (#1162).

Drives the REAL session-hud.js + session-hud-controller.js + topology-render.js
in node against a stub DOM (tests/fixtures/session_hud_detent_harness.mjs):
however expanded cards are closed — chord collapse-all, per-card close,
Enter/Space, or the drawer itself closing (Alt+P) — the drawer returns to the
peek height it had before the expand instead of staying at the half detent.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "fixtures" / "session_hud_detent_harness.mjs"
STATIC_JS = ROOT / "agentwire" / "static" / "js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_hud_returns_to_peek_after_every_close_path():
    proc = subprocess.run(
        ["node", "--no-warnings", str(HARNESS), str(STATIC_JS)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    results = json.loads(proc.stdout)
    failed = [name for name, ok in results.items() if ok is not True]
    assert not failed, failed
    assert len(results) >= 12, "harness stopped reporting checks"
