"""A commit message / PR body is data for git and gh — rules must not read it as a command.

Two halves, both load-bearing (false-accept and false-reject): honest prose about
a rule no longer trips the rule, and every way a message value could actually
run something — or sit next to a real command — still reaches the rules.
"""

from pathlib import Path

import pytest

from agentwire.safety._core import check_command, load_config, mask_prose_operands

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"
_RF = "-r" + "f"
SQL = "DROP " + "TABLE"


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    assert c.get("bashToolPatterns"), "bundled rules failed to load"
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


PROSE = {
    "commit-multiline": 'git commit -q -m "plain rm yields when recoverable\n\nrm tracked.txt is destructive only when..."',
    "commit-sql-word": f'git commit -m "docs: {SQL} still blocks"',
    "commit-single-quote": "git commit -m 'mentions $(not run) and rm x'",
    "commit-two-messages": f'git commit -m "title" -m "body: {SQL}"',
    "commit-am": f'git commit -am "{SQL} text"',
    "commit-equals": f'git commit --message="{SQL} text"',
    "pr-body": f'gh pr create --title t --body "replay: rm with flags, {SQL}, git clean"',
    "pr-title-equals": f'gh pr create --title="{SQL} guard" --body-file b.md',
    "issue-comment": f'gh issue comment 940 --body "{SQL} is blocked by the rule"',
    "tag": f'git tag -a v1 -m "{SQL} release notes"',
}

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


def test_prose_is_not_read_as_a_command(cfg):
    wrongly_stopped = [n for n, c in PROSE.items() if check_command(c, cfg)["decision"] != "allow"]
    assert not wrongly_stopped, wrongly_stopped


def test_real_commands_beside_or_inside_messages_are_still_read(cfg):
    wrongly_allowed = [n for n, c in STILL_READ.items() if check_command(c, cfg)["decision"] == "allow"]
    assert not wrongly_allowed, wrongly_allowed


def test_only_pure_quoted_values_are_masked():
    assert mask_prose_operands('git commit -m "a b"') == 'git commit -m "PROSE"'
    assert mask_prose_operands('git commit -m "a $(x) b"') == 'git commit -m "a $(x) b"'
    assert mask_prose_operands("ls -m \"a b\"") == "ls -m \"a b\""
