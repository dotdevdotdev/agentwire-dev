"""Behavioural guard for portal hotkeys: a hotkey's character must never reach a box.

Drives the REAL hotkeys.js + dead-key-suppressor.js in node against a stub DOM
(tests/fixtures/hotkeys_harness.mjs). Covers the regression where Option+`
left a stray ` in visible inputs because the suppressor only cleaned xterm's
hidden textarea. The native macOS composition itself can't be reproduced
headlessly — this pins the contract the modules own: claim the key, arm for
dead keys, swallow composition/input events, restore the pre-chord value.
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


@pytest.mark.parametrize("check", [
    "claimed", "fired_once", "repeat_ignored", "cmd_variant_untouched", "bare_untouched",
    "blurred_for_commit", "value_restored", "app_listeners_never_saw_it",
    "plain_typing_untouched", "expired_passes_through", "non_dead_no_blur",
    "when_false_leaves_key", "when_true_claims_key", "allow_repeat",
])
def test_hotkey_contract(results, check):
    assert results[check] is True

