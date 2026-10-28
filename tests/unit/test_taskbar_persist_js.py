"""Taskbar persistence must survive WebKit's page lifecycle (#1173).

Drives the REAL taskbar-persist.js in node (tests/fixtures/taskbar_persist_harness.mjs).
The old design froze saves on `beforeunload`, closed every window, and never thawed:
a bfcache restore (`pageshow` persisted — WebKit does this aggressively) or a
cancelled navigation left the page with no windows and saving off for good.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "fixtures" / "taskbar_persist_harness.mjs"
STATIC_JS = ROOT / "agentwire" / "static" / "js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_taskbar_persistence_lifecycle():
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_JS)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    results = json.loads(proc.stdout)
    failed = [name for name, ok in results.items() if ok is not True]
    assert not failed, failed
    assert len(results) >= 5, "harness stopped reporting checks"
