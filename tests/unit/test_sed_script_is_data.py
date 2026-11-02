"""A sed substitution script is data for the read-only-path ladder, not a path operand.

``sed -i 's|a|sharp/dist/index.cjs|' scratch.mjs`` rewrites ``scratch.mjs``; the
text ``dist/`` lives in the script. Both halves are load-bearing: the script is
released, and every way the guarded path is actually a TARGET (the file operand,
a redirect, a ``w`` flag, an ``e`` command, a substitution that executes) still
blocks.
"""


import pytest

from damage_control.engine import check_command, load_config
from damage_control.engine import bundled_rule_sources

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = bundled_rule_sources()


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    assert c.get("readOnlyPaths"), "bundled rules failed to load"
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


REAL_TARGETS = {
    "file-operand-dist": "sed -i '' 's/a/b/' dist/app.js",
    "file-operand-modules": "sed -i 's/a/b/' node_modules/pkg/index.js",
    "write-flag": "sed -i 's/a/b/w dist/out.js' notes.txt",
    "execute-flag": "sed -i 's/a/b/e' dist/x.js",
    "redirect": "sed 's|x|y|' notes.txt > dist/out.js",
    "tee": "cat a.txt | sed 's|x|y|' | tee dist/o.js",
    "script-with-command": "sed -i '1e rm -rf dist/' notes.txt",
    "substitution-in-script": 'sed -i "s|a|$(echo dist/x)|" notes.txt',
}


def test_a_guarded_path_that_is_really_a_target_still_blocks(cfg):
    wrongly_allowed = [
        n for n, c in REAL_TARGETS.items() if check_command(c, cfg)["decision"] == "allow"
    ]
    assert not wrongly_allowed, wrongly_allowed
