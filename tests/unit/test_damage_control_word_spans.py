"""A `)` / `;` / `&&` inside a word's DATA never hides the verb (#1119).

`A=${x:-)} rm -rf /tmp/x` was ALLOWED while `rm -rf /tmp/x` was blocked: the
scanner cut `${x:-)}` at its separator (or the case-arm finder took its `)` as a
pattern terminator) and the leftover `}` became the command, leaving `rm` an
argument. bash reads `${...}`, `$'...'` and `name[...]=` as ONE word. These
tests pin the OPERATION via a differential: the decision for the plain command
equals the decision with the construct spliced into every position that could
swallow the verb, in every wrapper, in every enforcement state. A construct that
holds a command substitution is ask-tier by itself, so it must only never ALLOW.

Loads the bundled rules and tooldefs, never the live ``~/.agentwire`` copies.
"""

from pathlib import Path

import pytest

from agentwire.safety._core import (
    _scan_masked_tokens,
    check_command,
    load_config,
    masked_subcommands,
    strip_command_opener_words,
)

pytestmark = pytest.mark.real_agentwire_home

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"

DANGEROUS = [
    "rm -rf /tmp/x",
    "sudo rm -rf /tmp/x",
    "git push --force origin main",
    "git reset --hard HEAD~3",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda1",
    "chmod -R 777 /",
    "echo x >| ~/.agentwire/damagecontrol.yml",
    "ssh host rm -rf /tmp/x",
]

# Word values that carry a paren / separator / newline as DATA.
CONSTRUCTS = [
    "${x:-)}",
    "${x:-a)b}",
    "${x:-(}",
    "${x:-;}",
    "${x:-&&}",
    "${x:-|}",
    "${x:-\n}",
    "${x//)/}",
    "${y:-${z}}",
    "${x:-}}",
    '${x:-"}"}',
    "${x:-'}'}",
    "${x:-\\}}",
    '${x:-")"}',
    '"${x:-)}"',
    "$'a)b'",
    "'a)b'",
    '"a)b"',
    "a\\)b",
    "$'a;b'",
]
# Ask-tier on their own (a substitution; a `\'` inside `$'...'` that shlex cannot
# parse, so the ambiguity check fails closed). The verb must still never be allowed.
ASK_TIER = ["${x:-$(echo ))}", "${x:-`echo )`}", "$'a\\'b;c'"]
# Only an assignment word may carry a `[...]` subscript with separators in it.
SUBSCRIPTS = ["a[)]=1", "a[;]=1", "a[&&]=1", "a[x y]=1", "a[|]+=1"]

# Positions that can swallow the verb. {w} = the construct, {c} = the command.
TEMPLATES = {
    "assign-prefix": "A={w} {c}",
    "assign-prefix-second": "A=1 B={w} {c}",
    "assign-prefix-first": "A={w} B=1 {c}",
    "after-arg": "echo {w}; {c}",
    "arg-then-and": "echo {w} && {c}",
    "operand-after-verb": "{c} {w}",
}
SUBSCRIPT_TEMPLATES = {"subscript-prefix": "{w} {c}"}
SUBSCRIPT_ASK_TIER = ["a[$((1))]=1"]

WRAPPERS = {
    "top": "{c}",
    "if": "if true; then {c}; fi",
    "while": "while true; do {c}; done",
    "for": "for i in 1; do {c}; done",
    "case": "case x in a) {c};; esac",
    "function": "f() { {c}; }; f",
    "subshell": "( {c} )",
    "brace": "{ {c}; }",
}

HARMLESS = ["echo hi", "ls -la", "git status"]


@pytest.fixture(scope="module")
def config():
    cfg = load_config(RULES_DIR, TOOLDEFS_DIR)
    assert cfg["bashToolPatterns"], "rules failed to load"
    cfg["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return cfg


def _decision(config, command):
    return check_command(command, config)["decision"]


def _cases(constructs, templates):
    for command in DANGEROUS:
        for w in constructs:
            for tname, template in templates.items():
                for wname, wrapper in WRAPPERS.items():
                    spliced = template.replace("{w}", w).replace("{c}", command)
                    yield tname, wname, command, w, wrapper.replace("{c}", spliced)


def test_every_dangerous_command_is_refused_at_top_level(config):
    """Guard the guard: a differential over allowed commands proves nothing."""
    bad = [c for c in DANGEROUS if _decision(config, c) == "allow"]
    assert not bad, bad


def test_construct_in_any_position_keeps_the_top_level_decision(config):
    top = {c: _decision(config, c) for c in DANGEROUS}
    bad = []
    for tname, wname, command, w, script in _cases(CONSTRUCTS, TEMPLATES):
        got = _decision(config, script)
        if got != top[command]:
            bad.append((tname, wname, w, command, top[command], got))
    assert not bad, bad


def test_subscript_assignment_keeps_the_top_level_decision(config):
    top = {c: _decision(config, c) for c in DANGEROUS}
    bad = []
    for tname, wname, command, w, script in _cases(SUBSCRIPTS, SUBSCRIPT_TEMPLATES):
        got = _decision(config, script)
        if got != top[command]:
            bad.append((tname, wname, w, command, top[command], got))
    assert not bad, bad


def test_ask_tier_construct_never_allows_the_verb(config):
    bad = []
    cases = list(_cases(ASK_TIER, TEMPLATES)) + list(_cases(SUBSCRIPT_ASK_TIER, SUBSCRIPT_TEMPLATES))
    for tname, wname, command, w, script in cases:
        if _decision(config, script) == "allow":
            bad.append((tname, wname, w, command))
    assert not bad, bad


@pytest.mark.parametrize("state", ["kill-switch-off", "unattended", "kill-switch-off-unattended"])
def test_differential_holds_with_kill_switch_off_and_unattended_on(config, monkeypatch, state):
    enabled = not state.startswith("kill-switch-off")
    cfg = dict(config, safety=dict(config["safety"], enabled=enabled))
    if state.endswith("unattended"):
        monkeypatch.setenv("AGENTWIRE_UNATTENDED", "1")
    else:
        monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)
    top = {c: _decision(cfg, c) for c in DANGEROUS}
    templates = {k: TEMPLATES[k] for k in ("assign-prefix", "after-arg")}
    bad = []
    for tname, wname, command, w, script in _cases(CONSTRUCTS, templates):
        got = _decision(cfg, script)
        if got != top[command]:
            bad.append((state, tname, wname, w, command, top[command], got))
    assert not bad, bad


def test_harmless_commands_around_every_construct_stay_allowed(config):
    bad = []
    for command in HARMLESS:
        for w in CONSTRUCTS + SUBSCRIPTS:
            templates = SUBSCRIPT_TEMPLATES if w in SUBSCRIPTS else TEMPLATES
            for template in templates.values():
                script = template.replace("{w}", w).replace("{c}", command)
                for wname, wrapper in WRAPPERS.items():
                    full = wrapper.replace("{c}", script)
                    if _decision(config, full) != "allow":
                        bad.append((wname, full))
    assert not bad, bad


def test_scanner_keeps_each_construct_one_word():
    """`;`/`&&`/`)` inside the span must not cut the token or the subcommand."""
    bad = []
    for w in CONSTRUCTS + ASK_TIER:
        subs = _scan_masked_tokens(f"A={w} rm -rf /tmp/x")
        if len(subs) != 1 or [t[0] for t in subs[0]][1:] != ["rm", "-rf", "/tmp/x"]:
            bad.append((w, subs))
    assert not bad, bad


def test_unclosed_span_does_not_swallow_the_rest(config):
    """An unterminated `${` / `$'` is not a span: the verb after it is still seen."""
    bad = [
        s
        for s in ("echo ${x:-a; rm -rf /tmp/x", "echo $'a; rm -rf /tmp/x", "echo a[b; rm -rf /tmp/x]=1")
        if _decision(config, s) == "allow"
    ]
    assert not bad, bad


def test_subscript_without_assignment_does_not_swallow_a_separator(config):
    """`a[x;rm -rf /tmp/x]` has no `=` after the `]`: bash splits at the `;`."""
    assert _decision(config, "a[x;rm -rf /tmp/x]") != "allow"


def test_subscript_without_assignment_stays_two_subcommands():
    """The raw haystacks would still see the verb, so pin the split itself."""
    subs = _scan_masked_tokens("a[x;rm -rf /tmp/x]")
    assert [t[0] for t in subs[-1]][0].startswith("rm"), subs


def test_data_parens_are_restored_in_masked_words():
    """`_DATA_PAREN` is scanner-internal: no rule may ever see it."""
    bad = [
        (w, words)
        for w in ("a\\)b", "'a)b'", "${x:-)}", "$'a)b'")
        for words in masked_subcommands(f"echo {w} c")
        if "\x02" in words
    ]
    assert not bad, bad
    assert masked_subcommands("echo a\\)b c") == ["echo a)b c"]


def test_walker_without_quote_info_skips_parens_inside_spans():
    """Words from shlex-based walkers carry no quotes: the span-aware finder is all they have."""
    bad = [
        w
        for w in ("A=${x:-)}", "A=${x:-a)b}", "A=${y:-${z}}", "a[)]=1", "A=${x:-$(echo ))}")
        if strip_command_opener_words([w, "rm", "-rf", "/x"])[0] != w
    ]
    assert not bad, bad
    # a real arm terminator after the span is still found
    assert strip_command_opener_words(["${x:-)})", "rm", "-rf"]) == ["rm", "-rf"]
