"""Behavioural guard for portal hotkeys: a hotkey's character must never reach a box.

Drives the REAL hotkeys.js + dead-key-residue.js in node against a stub DOM
(tests/fixtures/hotkeys_harness.mjs). The event order it replays was MEASURED in
Safari 26: Option down, composition puts the accent in the field, THEN the
chord's keydown arrives — so the contract is: claim the key, and restore the
value captured when Option went down. The native composition itself can't be
reproduced headlessly.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "fixtures" / "hotkeys_harness.mjs"
STATIC_JS = ROOT / "agentwire" / "static" / "js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture(scope="module")
def results():
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_JS)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(proc.stdout)


def test_hotkey_contract(results):
    failed = [name for name, ok in results.items() if ok is not True]
    assert not failed, failed
    assert len(results) >= 25, "harness stopped reporting checks"
