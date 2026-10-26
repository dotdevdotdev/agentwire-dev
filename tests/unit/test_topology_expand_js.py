"""Behavioural guard for the Session HUD's multi-expand (#1104).

Drives the REAL topology-render.js in node against a stub DOM
(tests/fixtures/topology_expand_harness.mjs): any number of child terminals can
be live at once, expand-all honours the cap and skips self/ghost cards, and a
card is keyboard-toggleable without keys from inside its terminal toggling it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "fixtures" / "topology_expand_harness.mjs"
STATIC_JS = ROOT / "agentwire" / "static" / "js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_topology_multi_expand():
    proc = subprocess.run(
        ["node", "--no-warnings", str(HARNESS), str(STATIC_JS)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    results = json.loads(proc.stdout)
    failed = [name for name, ok in results.items() if ok is not True]
    assert not failed, failed
    assert len(results) >= 9, "harness stopped reporting checks"
