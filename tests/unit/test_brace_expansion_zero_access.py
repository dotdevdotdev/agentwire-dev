"""A secret path written inside a shell brace expansion is still the secret path (#1067).

``cat {.env,x}`` reads ``.env`` — bash expands the group before cat runs — but
the rule matched the literal text and saw no path. Both halves matter: every
expanded spelling must block, and braces that are NOT expansions (``${var}``,
a group holding whitespace, a group with no comma) must not start blocking.
"""

from pathlib import Path

import pytest

from agentwire.safety._core import brace_variants, check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    assert c.get("zeroAccessPaths"), "bundled rules failed to load"
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


EXPANDS_TO_A_SECRET = {
    "plain": "cat {.env,x}",
    "second-alternative": "cat {x,.env}",
    "home-dir": "cat ~/.agentwire/{.env,x}",
    "prefix-group": "cat .{env,x}",
    "empty-alternative": "cat {.,}{env,x}",
    "nested": "cat {{.env,a},b}",
    "glob-pattern": "cat {a.key,b}",
    "two-groups": "cat {a,b}/{x,.env}",
}

NOT_EXPANSIONS = {
    "dollar-brace": "echo ${HOME}",
    "no-comma": "echo {a}",
    "whitespace-group": "awk '{print $1,$2}' notes.txt",
    "benign-expansion": "cat {a,b}.txt",
    "dollar-brace-pair": "echo \"${A},${B}\"",
}


def test_brace_expanded_secret_paths_block(cfg):
    wrongly_allowed = [n for n, c in EXPANDS_TO_A_SECRET.items() if check_command(c, cfg)["decision"] == "allow"]
    assert not wrongly_allowed, wrongly_allowed


def test_braces_that_are_not_expansions_stay_allowed(cfg):
    wrongly_stopped = [n for n, c in NOT_EXPANSIONS.items() if check_command(c, cfg)["decision"] != "allow"]
    assert not wrongly_stopped, wrongly_stopped


def test_an_expansion_bomb_fails_closed(cfg):
    bomb = "cat " + "{a,b}" * 12
    assert brace_variants(bomb) is None
    assert check_command(bomb, cfg)["decision"] == "block"
    assert brace_variants("ls plain") == []
