"""A command inside a compound-command body is judged like the same command at top level (#1112).

``case x in a|b) rm -rf /tmp/x;; esac`` was ALLOWED while ``rm -rf /tmp/x`` was
blocked: the anchored rules only match at a command position and the chain
stopped at the first non-wrapper word (``case``, ``then``, ``{``, ``a|b)`` …).
These tests pin the OPERATION via a differential: for every rule-triggering
command x compound wrapper, the decision inside the wrapper equals the decision
at top level. Controls: harmless commands in the same wrappers stay allowed, and
the verb in a case PATTERN or a string is data.

Loads the bundled rules and tooldefs, never the live ``~/.agentwire`` copies.
"""

from pathlib import Path

import pytest

from agentwire.safety._core import check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"

DANGEROUS = [
    "rm -rf /tmp/x",
    "rm -rf ~/x",
    "git push --force origin main",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda1",
    "chmod -R 777 /",
    "cd ~/.agentwire && touch damagecontrol.yml",
    "echo x >| ~/.agentwire/damagecontrol.yml",
]

WRAPPERS = {
    "case": "case x in a|b) {c};; esac",
    "case-leading-paren": "case x in (a) {c};; esac",
    "case-newlines": "case x in\n a)\n {c}\n ;;\nesac",
    "case-fallthrough": "case x in a) true;& b) {c};;& c) true;; esac",
    "case-second-arm": "case x in a) true;; b|c) {c};; esac",
    "case-quoted-glob-pattern": "case x in 'a)'|\"b\"|[a-z]*|*.sh) {c};; esac",
    "case-spaced-quoted-pattern": "case x in 'a b') {c};; esac",
    "case-multi-command-body": "case x in a) echo hi; {c} && echo done;; esac",
    "case-unterminated-last-arm": "case x in a) {c} esac",
    "case-in-function": "f() { case x in a) {c};; esac; }; f",
    "case-nested": "case x in a) case y in b) {c};; esac;; esac",
    "if": "if true; then {c}; fi",
    "if-else": "if false; then true; else {c}; fi",
    "elif": "if false; then true; elif true; then {c}; fi",
    "if-condition": "if {c}; then true; fi",
    "while": "while true; do {c}; done",
    "until": "until false; do {c}; done",
    "for": "for i in 1 2; do {c}; done",
    "select": "select i in a; do {c}; done",
    "arith-for": "for ((i=0;i<1;i++)); do {c}; done",
    "brace-group": "{ {c}; }",
    "subshell": "( {c} )",
    "subshell-glued": "({c})",
    "function": "f() { {c}; }; f",
    "function-keyword": "function f { {c}; }; f",
    "negation": "! {c}",
    "time": "time {c}",
    "coproc": "coproc {c}",
    "coproc-named": "coproc NAME { {c}; }",
}

HARMLESS = ["echo hi", "ls -la", "git status"]

# The wrapper nests the case itself; $( ) is a deliberate ask-tier shape of its
# own at top level too, so it is compared against the plain substitution.
SUBSTITUTION = "echo $({inner})"


@pytest.fixture(scope="module")
def config():
    cfg = load_config(RULES_DIR, TOOLDEFS_DIR)
    assert cfg["bashToolPatterns"], "rules failed to load"
    cfg["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return cfg


def _decision(config, command):
    return check_command(command, config)["decision"]


def test_every_dangerous_command_is_refused_at_top_level(config):
    """Guard the guard: a differential over allowed commands proves nothing."""
    bad = [c for c in DANGEROUS if _decision(config, c) == "allow"]
    assert not bad, bad


def test_wrapped_decision_equals_top_level(config):
    bad = []
    for command in DANGEROUS:
        top = _decision(config, command)
        for name, wrapper in WRAPPERS.items():
            got = _decision(config, wrapper.replace("{c}", command))
            if got != top:
                bad.append((name, command, top, got))
    assert not bad, bad


def test_case_inside_substitution_equals_plain_substitution(config):
    bad = []
    for command in DANGEROUS:
        plain = _decision(config, SUBSTITUTION.format(inner=command))
        inner = WRAPPERS["case"].replace("{c}", command)
        got = _decision(config, SUBSTITUTION.format(inner=inner))
        if got == "allow" or got != plain:
            bad.append((command, plain, got))
    assert not bad, bad


def test_harmless_commands_stay_allowed_in_every_wrapper(config):
    bad = []
    for command in HARMLESS:
        for name, wrapper in WRAPPERS.items():
            got = _decision(config, wrapper.replace("{c}", command))
            if got != "allow":
                bad.append((name, command, got))
    assert not bad, bad


@pytest.mark.parametrize("script", [
    "case x in rm) echo hi;; esac",
    "case rm in a) echo hi;; esac",
    "case \"rm -rf /tmp/x\" in a) echo hi;; esac",
    "case x in a) echo 'rm -rf /tmp/x';; esac",
    "case x in a) echo \"rm -rf /tmp/x\";; esac",
    "case x in a) grep -c rm -rf notes.txt;; esac",
    "if true; then echo rm -rf /tmp/x; fi",
    "for rm in a b; do echo hi; done",
])
def test_verb_as_data_in_a_compound_stays_allowed(config, script):
    assert _decision(config, script) == "allow"
