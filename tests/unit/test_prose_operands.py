"""A commit message / PR body is data for git and gh — rules must not read it as a command.

Two halves, both load-bearing (false-accept and false-reject): honest prose about
a rule no longer trips the rule, and every way a message value could actually
run something — or sit next to a real command — still reaches the rules.
"""


import pytest
from damage_control.engine import bundled_rule_sources, check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = bundled_rule_sources()
_RF = "-r" + "f"
SQL = "DROP " + "TABLE"


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    assert c.get("bashToolPatterns"), "bundled rules failed to load"
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


# Each is a real command (or can run one) and must NOT be hidden by the masking.
STILL_READ = {
    "real-sql-after-commit": f'git commit -m "x" && psql -c "{SQL} users"',
    "real-sql-newline": f'git commit -m "x"\npsql -c "{SQL} users"',
    "substitution-in-body": f"gh pr create --body \"$(psql -c '{SQL} users')\"",
    "backtick-in-message": f'git commit -m "`psql -c \\"{SQL} users\\"`"',
    "echo-is-not-masked": f'echo "{SQL} users" | psql',
    "other-git-verb": f'git stash && psql -c "{SQL} users"',
    "shell-wrapper": f"sh -c 'psql -c \"{SQL} users\"'",
    "rm-after-message": f'git commit -m "ok"; rm {_RF} build',
    "unbalanced": f'git commit -m "{SQL} users',
}


def test_real_commands_beside_or_inside_messages_are_still_read(cfg):
    wrongly_allowed = [
        n for n, c in STILL_READ.items() if check_command(c, cfg)["decision"] == "allow"
    ]
    assert not wrongly_allowed, wrongly_allowed
