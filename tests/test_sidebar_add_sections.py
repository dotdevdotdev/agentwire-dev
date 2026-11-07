"""#1300 — one sidebar section that throws must not abort portal init.

desktop.js init() mounts every section and then binds the global hotkeys; an
uncaught throw from any addSection used to skip everything after it. The
isolation helper is a pure ES module, so it is exercised under real node.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).parent.parent / "agentwire/static/js/sidebar/add-sections.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

SCRIPT = """
import { addSectionsIsolated } from %s;
const mounted = [];
const errors = [];
const sidebar = { addSection(id) { if (id === 'bad') throw new Error('boom'); mounted.push(id); } };
const failed = addSectionsIsolated(
  sidebar,
  [['a', {}], ['bad', {}], ['c', {}]],
  (id, err) => errors.push([id, err.message]),
);
console.log(JSON.stringify({ mounted, errors, failed }));
"""


def test_failing_section_is_reported_and_later_sections_still_mount():
    script = SCRIPT % json.dumps(MODULE.as_uri())
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(out)
    assert result == {
        "mounted": ["a", "c"],
        "errors": [["bad", "boom"]],
        "failed": ["bad"],
    }
