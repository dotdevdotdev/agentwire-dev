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

EXECUTING_CORPUS = [
    f"true &&\\\n{W} x", f"true ;\\\n {W} x",
    f"f(){{ {W} x; }}; f", f"function f {{ {W} x; }}; f", f"coproc X {{ {W} x; }}",
    f"case a in a) case b in b) {W} x;; esac;; esac",
    f">f {W} x", f"<f {W} x", f"2>&1 {W} x", f">>f {W} x", f"> f {W} x", f"2>/dev/null {W} x",
    f"source <({W} x)", f". <({W} x)", f"source <(echo {W} x)",
    f"cat <<EOF | bash\n{W} x\nEOF", f"echo {W} x | sh", f"bash <<< {W}", f"bash -c {W}",
    f"echo {W} x | zsh", f"echo {W} x | xargs sh -c", f"echo {W} x | source /dev/stdin",
    f"watch '{W} x'", f"trap '{W} x' EXIT", f"cat <<'EOF' | sh\n{W} x\nEOF",
    f"echo '{W} x' | sh", f"bash <<< '{W} x'", f"runuser -c '{W} x' u",
    f"tmux new-session '{W} x'", f"env -S '{W} x'",
    "$'ev''al' x", f"$\"{W}\" x",
    f"sudo -u root {W} x", "(eval x)".replace("eval", W), f"{{{W} x; }}".replace("{", "{ ", 1),
    f"xargs -I{{}} {W} {{}}", f"find . -exec {W} {{}} \\;", f"find . -exec sh -c '{W} x' \\;",
    f"nohup -- {W} x",
]


DATA_CORPUS = [
    f"xargs -I{{}} grep {W} {{}}", f"xargs -n1 grep {W}", f"xargs -0 grep {W}",
    f"sudo -E grep {W} f", f"env -i grep {W} f", f"nohup -- grep {W} f",
    f"find . -exec grep -l {W} {{}} \\;", f"find . -exec echo {W} \\;", f"find . -name {W}",
    f"watch echo {W}", f"command -v {W}", f"type {W}", f"which {W}", f"help {W}",
    f"echo {W} | grep {W}", f"echo {W} | wc", f"echo {W} > f", f"cat f | grep {W}",
    f"echo {W} x | tee out.txt", f"echo '{W} x' | grep x", f"cat <<'EOF' | grep x\n{W} x\nEOF",
    f"echo '{W} x' >> notes.md",
]



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


def _cfg_state(config, enabled=True):
    return dict(config, safety=dict(config["safety"], enabled=enabled))


def test_corpus_executing_spellings_conceal_and_are_never_allowed(cfg):
    """Regression-vs-main invariant: main asked on (or a stricter rule blocked) every
    spelling bash executes, so none may fall to allow; the quote-split, quoted-verb and
    payload spellings main MISSED are held to the same bar."""
    bad = [
        c for c in EXECUTING_CORPUS
        if ambiguity_conceals_verb(c) is None or check_command(c, cfg)["decision"] == "allow"
    ]
    assert not bad, bad


def test_corpus_data_mentions_allow(cfg):
    bad = [
        c for c in DATA_CORPUS
        if ambiguity_conceals_verb(c) is not None or check_command(c, cfg)["decision"] != "allow"
    ]
    assert not bad, bad


def test_unattended_executing_spellings_stay_unverifiable(cfg, monkeypatch):
    monkeypatch.setenv("AGENTWIRE_UNATTENDED", "1")
    bad = []
    for c in EXECUTING_CORPUS:
        r = check_command(c, cfg)
        if r["decision"] == "allow" or (r["decision"] == "ask" and not r.get("unverifiable")):
            bad.append(c)
    assert not bad, bad
    assert not [c for c in DATA_CORPUS if check_command(c, cfg)["decision"] != "allow"]


def test_kill_switch_off_has_no_eval_specific_behaviour(cfg):
    """The documented off switch allows everything, executing or not: no verdict here may
    depend on the word, so turning it off cannot leave a half-guarded eval path."""
    off = _cfg_state(cfg, enabled=False)
    got = {check_command(c, off)["decision"] for c in EXECUTING_CORPUS + DATA_CORPUS}
    assert got == {"allow"}, got


def test_padded_command_cannot_hide_an_executing_spelling(cfg):
    """Composition with the size budget (#1108/#1111): padding stays under the per-line
    limit, then over it; neither shape may end in allow."""
    from agentwire.safety import _core

    bad = []
    for n in (200, _core.MAX_LINE_CHARS - 60, _core.MAX_LINE_CHARS + 10):
        pad = "a" * n
        for c in (f"echo {pad}; {W} \"$v\"", f"echo {pad}\n{W} x", f"{W} \"$v\"; echo {pad}"):
            if check_command(c, cfg)["decision"] == "allow":
                bad.append((n, c[:20], c[-20:]))
    assert not bad, bad


PINS = {
    # the option-value skip: a value-taking flag must not hide the verb behind it
    "sudo-value-option": f"sudo -u root {W} x",
    "env-value-option": f"env -u HOME {W} x",
    "xargs-value-option": f"xargs -n1 {W} x",
    "timeout-duration": f"timeout 5 {W} x",
    "nice-value": f"nice -n 5 {W} x",
    # glued opener characters
    "glued-paren": f"({W} x)",
    "glued-brace": f"{{ {W} x; }}",
    "glued-paren-chain": f"a && ({W} x)",
    # redirect forms before the verb
    "redirect-fd-dup": f"2>&1 {W} x",
    "redirect-glued": f">out {W} x",
    "redirect-spaced": f"> out {W} x",
    # payload re-scans
    "shell-c-payload": f"bash -c '{W} \"$v\"'",
    "su-c-payload": f"su -c '{W} x' u",
    "env-S-payload": f"env -S '{W} x'",
    "trap-payload": f"trap '{W} x' EXIT",
    "watch-payload": f"watch '{W} x'",
    "tmux-run-payload": f"tmux new-session '{W} x'",
    "find-exec-vector": f"find . -exec {W} {{}} \\;",
    "ssh-remote": f"ssh host {W} x",
    # fed programs
    "pipe-to-shell": f"echo {W} x | sh",
    "pipe-to-xargs-shell": f"echo {W} x | xargs sh -c",
    "quoted-heredoc-pipe-shell": f"cat <<'EOF' | sh\n{W} x\nEOF",
    "here-string-shell": f"bash <<< '{W} x'",
    "procsub-body": f"cat <({W} x)",
    "source-procsub-feed": f"source <(echo {W} x)",
    "line-continuation": f"true &&\\\n{W} x",
}


def test_each_pinned_shape_conceals():
    bad = [n for n, c in PINS.items() if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


QUERIES_AND_ARGUMENTS = {
    "command-v": f"command -v {W}",
    "type": f"type {W}",
    "which": f"which {W}",
    "help": f"help {W}",
    "xargs-boolean-flag": f"xargs -0 grep {W}",
    "sudo-boolean-flag": f"sudo -E grep {W} f",
    "env-boolean-flag": f"env -i grep {W} f",
    "nohup-double-dash": f"nohup -- grep {W} f",
    "find-grep-vector": f"find . -exec grep -l {W} {{}} \\;",
    "find-name": f"find . -name {W}",
    "watch-echo": f"watch echo {W}",
    "tmux-send-keys": f'tmux send-keys -t x "{W} foo" Enter',
}


def test_queries_and_arguments_do_not_conceal():
    bad = [n for n, c in QUERIES_AND_ARGUMENTS.items() if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad
