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
    "git reset --hard HEAD~3",
    "git clean -fdx",
    "kill -9 -1",
    "sudo rm -rf /tmp/x",
    "ssh host rm -rf /tmp/x",
    "ssh host 'rm -rf /tmp/x'",
    "time -p cd ~/.agentwire && touch damagecontrol.yml",
    "nohup cd ~/.agentwire && touch damagecontrol.yml",
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
    "until-condition": "until {c}; do true; done",
    "elif-condition": "if false; then true; elif {c}; then true; fi",
    "while-condition": "while {c}; do true; done",
    # glued openers/closers (#1112 review): `(` `)` always break words in bash
    "subshell-case": "(case x in a) {c};; esac)",
    "subshell-spaced-case": "( case x in a) {c};; esac )",
    "func-glued-brace": "f(){ {c}; }; f",
    "func-keyword-glued": "function f(){ {c}; }; f",
    "func-spaced-parens": "f ( ) { {c}; }; f",
    "case-first-arm-glued-body": "case x in a){c};; esac",
    "case-second-arm-glued-body": "case x in a)true;;b){c};; esac",
    "case-glued-leading-paren": "case x in (a){c};; esac",
    "then-glued-paren": "if true; then({c}); fi",
    "do-glued-paren": "for i in 1; do({c}); done",
    "negated-subshell": "!({c})",
    "case-no-in-header": "case x a) {c};; esac",
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


STATES = {
    "default": (True, False),
    "kill-switch-off": (False, False),
    "unattended": (True, True),
    "kill-switch-off-unattended": (False, True),
}


def test_differential_holds_with_kill_switch_off_and_unattended_on(config, monkeypatch):
    bad = []
    for state, (enabled, unattended) in STATES.items():
        cfg = dict(config, safety=dict(config["safety"], enabled=enabled))
        if unattended:
            monkeypatch.setenv("AGENTWIRE_UNATTENDED", "1")
        else:
            monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)
        for command in DANGEROUS:
            top = _decision(cfg, command)
            for name, wrapper in WRAPPERS.items():
                got = _decision(cfg, wrapper.replace("{c}", command))
                if got != top:
                    bad.append((state, name, command, top, got))
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
    # bash reserves `{` and the keywords only as whole words, so these are plain
    # words and must not be read as openers
    "echo {rm,ls} -rf /tmp/x",
    "{echo hi; }",
    "echo ${x} {a,b}",
    "[[ a == (x) ]] && echo hi",
    "grep -E '(a|b)' file",
    "echo '(rm -rf /tmp/x)'",
    "f() { echo hi; }; f",
    "f(){ echo hi; }; f",
    "case x in a)echo hi;; esac",
    "(echo hi)",
    "case x in a) echo hi;; esac",
    "case x in a|b|c) echo hi;; esac",
    "if true; then echo hi; fi",
])
def test_verb_as_data_in_a_compound_stays_allowed(config, script):
    assert _decision(config, script) == "allow"


def test_opener_stripper_exact_shapes():
    from agentwire.safety._core import strip_command_opener_words as strip

    cases = {
        # a case header with no pattern end (the `a|b)` first segment) is left whole
        "case x in a": ["case", "x", "in", "a"],
        "time -p rm -rf x": ["rm", "-rf", "x"],
        "if true": ["true"],
        "{rm -rf x": ["{rm", "-rf", "x"],
        "do{ rm": ["do{", "rm"],
        "echo then rm": ["echo", "then", "rm"],
        "f ( ) { rm x": ["rm", "x"],
        "case x in a)rm y": ["rm", "y"],
        "(rm y": ["rm", "y"],
    }
    bad = {k: strip(k.split()) for k, want in cases.items() if strip(k.split()) != want}
    assert not bad, bad


def test_concealment_sees_through_openers():
    from agentwire.safety._core import ambiguity_conceals_verb

    payload = 'ssh host "$(cat x)"'
    bad = [
        w for w in ("if true; then {c}; fi", "while true; do {c}; done", "! {c}")
        if ambiguity_conceals_verb(w.replace("{c}", payload)) is None
    ]
    assert ambiguity_conceals_verb(payload) is not None
    assert not bad, bad
