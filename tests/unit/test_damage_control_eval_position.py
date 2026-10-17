"""The dynamic-evaluation builtin conceals a verb only at COMMAND POSITION (#1115).

``_EVAL_RE`` used to be a whitespace-keyed word search, so it guarded the
phrasing: every commit message, issue body, grep pattern and ``send`` payload
that merely NAMED the builtin was an un-demotable ``unverifiable`` ask (a silent
block loop when unattended) — while real spellings the regex could not see
(``\\eval``, ``e''val``, ``bash -c 'eval "$x"'``) sailed through. Both halves
are pinned here as a differential: every executing (spelling x position x
wrapper) must conceal, every data mention must not.
"""

import itertools
import shlex
from pathlib import Path

import pytest

from agentwire.safety._core import (
    ambiguity_conceals_verb,
    check_command,
    load_config,
    normalize_subcommands,
)

pytestmark = pytest.mark.real_agentwire_home

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"

W = "ev" + "al"

SPELLINGS = [
    f'{W} "$v"',
    f"{W} $(cat f)",
    f"{W} x",
    f"builtin {W} x",
    f"command {W} x",
    f"\\{W} x",
    f"e''{W[1:]} x",
    f"ev\\{W[2:]} x",
    f'"{W}" x',
    f"'{W}' x",
]

# position templates: {c} is the executing command
POSITIONS = [
    "{c}",
    "a; {c}",
    "a && {c}",
    "a || {c}",
    "a | {c}",
    "a\n{c}",
    "( {c} )",
    "{{ {c}; }}",
    "if true; then {c}; fi",
    "if {c}; then :; fi",
    "if a; then :; else {c}; fi",
    "while true; do {c}; done",
    "until {c}; do :; done",
    "for i in 1; do {c}; done",
    "case a in a) {c};; esac",
    "case a in b) :;; a) {c};; esac",
    "f() {{ {c}; }}; f",
    "! {c}",
]

WRAPPERS = [
    "{c}",
    "sudo {c}",
    "sudo -E {c}",
    "env A=1 {c}",
    "time {c}",
    "nohup {c} &",
    "xargs {c}",
    "xargs -I{{}} {c}",
    "bash -c {q}",
    "sh -c {q}",
    "ssh host {c}",
    "find . -exec {c} \\;",
]

DATA_MENTIONS = {
    "echo": f"echo {W}",
    "echo-quoted": f'echo "run {W} now"',
    "echo-tail": f"echo foo {W}",
    "grep-squote": f"grep -rn '{W}' src",
    "grep-bare": f"grep {W} f | wc -l",
    "grep-E": f"grep -rnE '(^|[ ;]){W}( |$)' agentwire",
    "git-commit-m": f'git commit -m "fix {W} thing"',
    "git-commit-F": f"git commit -F msg.txt && echo {W}",
    "gh-body": f'gh issue create --title t --body "uses {W} here"',
    "gh-pr": f'gh pr create --body "Closes #1. {W} is gone"',
    "agentwire-msg": f'agentwire msg send --to x "the {W} rule"',
    "agentwire-send": f'agentwire send -s x "{W} \\"$v\\" is blocked"',
    "tmux-send-keys": f'tmux send-keys -t x "{W} foo" Enter',
    "heredoc-quoted": f"cat <<'EOF'\n{W} \"$v\"\nEOF",
    "heredoc-quoted-redirect": f"cat > notes.md <<'EOF'\nrepro: {W} $x\nEOF",
    "heredoc-unquoted": f"cat <<EOF\n{W} x\nEOF",
    "comment": f"echo x # {W} foo",
    "leading-comment": f"# {W} foo\nls",
    "sh-c-echo": f'sh -c "echo {W}"',
    "python-c": f"python3 -c 'print(\"{W}\")'",
    "command-v": f"echo hi && type {W}",
    "man": f"man {W}",
    "path-arg": f"ls docs/{W}.md",
    "after-eval-in-name": f"echo {W}uate",
}

_CMD_PREFIX = "x"  # keeps the generated name stable across pytest versions


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    assert c.get("bashToolPatterns"), "bundled rules failed to load"
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


def _executing_cases():
    for spell, pos, wrap in itertools.product(SPELLINGS, POSITIONS, WRAPPERS):
        # a wrapper around a compound body would only test the wrapper's quoting
        # twice; the cross-product covers it, so keep each axis independent
        if wrap != "{c}" and pos != "{c}":
            continue
        inner = pos.format(c=spell)
        yield wrap.format(c=inner, q=shlex.quote(inner))


def test_every_executing_spelling_conceals():
    bad = [c for c in _executing_cases() if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


def test_every_executing_spelling_is_ambiguous_to_the_normalizer():
    bad = [c for c in _executing_cases() if normalize_subcommands(c)[1] is None]
    assert not bad, bad


def test_executing_spelling_is_never_allowed_by_check_command(cfg):
    bad = [c for c in _executing_cases() if check_command(c, cfg)["decision"] == "allow"]
    assert not bad, bad


def test_data_mentions_do_not_conceal(cfg):
    bad = {
        n: (ambiguity_conceals_verb(c), normalize_subcommands(c)[1])
        for n, c in DATA_MENTIONS.items()
        if ambiguity_conceals_verb(c) is not None or normalize_subcommands(c)[1] is not None
    }
    assert not bad, bad


def test_data_mentions_are_not_unverifiable(cfg):
    bad = [n for n, c in DATA_MENTIONS.items() if check_command(c, cfg).get("unverifiable")]
    assert not bad, bad


def test_shell_fed_heredoc_body_still_conceals():
    """The body of a heredoc feeding a shell IS the program; the word there executes."""
    for c in (
        f"bash <<'EOF'\n{W} \"$v\"\nEOF",
        f"sh <<EOF\n{W} x\nEOF",
        f"ssh host <<'EOF'\n{W} \"$v\"\nEOF",
    ):
        assert ambiguity_conceals_verb(c) is not None, c


def test_data_then_executing_in_one_command_still_conceals():
    """A data mention must not shield a real call elsewhere in the same command."""
    for c in (
        f'echo {W}; {W} "$v"',
        f'git commit -m "{W}" && {W} "$v"',
        f"grep {W} f | {W} x",
    ):
        assert ambiguity_conceals_verb(c) is not None, c
