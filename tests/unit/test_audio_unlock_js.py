"""Behavioural guard for the Safari audio unlock (#1160).

Drives the REAL desktop-manager.js + voice/audio-unlock.js in node against a
stub AudioContext that mimics Safari (suspended; resume() settles only inside a
pointerdown/touchend/click, never keydown). Real Safari is NOT reproduced here.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "fixtures" / "audio_unlock_harness.mjs"
STATIC_JS = ROOT / "agentwire" / "static" / "js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_audio_unlock_contract():
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_JS)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    results = json.loads(proc.stdout)
    failed = [name for name, ok in results.items() if ok is not True]
    assert not failed, failed
    assert len(results) >= 8, "harness stopped reporting checks"
