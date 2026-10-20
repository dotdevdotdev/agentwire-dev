"""Every Bash block names the rule or check that produced it (#940).

The audit report bucketed 127 of 255 attended blocks as "(no rule_id recorded)":
the path ladders (zero-access / read-only / no-delete) and the control-plane
checks returned a block with a ``pattern`` but no ``id``, so nothing could rank
them or tell a rule from a check.
"""

from pathlib import Path

import pytest

from agentwire.safety._core import check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"

BLOCKS = {
    "zero-access": "cat server.key",
    "zero-access-env": "grep -c TOKEN .env",
    "read-only": "rm -f dist/app.js",
    "control-plane": "echo x > ~/.agentwire/damagecontrol.yml",
    "protected-command": "agentwire tasks promote x",
}


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


def test_every_block_carries_a_rule_or_check_id(cfg):
    unlabelled = {
        n: r["pattern"]
        for n, cmd in BLOCKS.items()
        for r in [check_command(cmd, cfg)]
        if r["decision"] != "block" or not r.get("id")
    }
    assert not unlabelled, unlabelled
