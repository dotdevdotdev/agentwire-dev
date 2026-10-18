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


EXECUTING_CORPUS += [
    f"echo {W} x | tee f | sh", f"echo {W} x | cat | sh", f"echo {W} x | sed s/a/b/ | tr a b | bash",
    f"bash <(echo {W} x)", f"source <(echo a; echo {W} x)", f"zsh <(printf '%s' '{W} x')",
    f"{{fd}}>f {W} x", f"&>f {W} x", f">|f {W} x", f"&>>f {W} x", f"<>f {W} x",
    f"<&- {W} x", f">&2 {W} x", f"3<<<foo {W} x", f"2>f {W} x",
    f"sudo -Eu root sh -c '{W} x'", f"sudo -r r {W} x", f"sudo -t t {W} x", f"env -P /bin {W} x",
    f"xargs -J % {W} %", f"xargs -R 1 {W}", f"time -o f {W} x", f"time -a -o f {W} x",
    f"uv run -q sh -c '{W} x'", f"uv run --no-project sh -c '{W} x'", f"uvx -q bash -c '{W} x'",
    f"npx -y sh -c '{W} x'", f"bunx -b sh -c '{W} x'", f"flock /tmp/l sh -c '{W} x'",
    f"chroot / sh -c '{W} x'", f"unshare -m sh -c '{W} x'", f"taskset 1 sh -c '{W} x'",
    f"chrt 1 sh -c '{W} x'", f"strace -f sh -c '{W} x'", f"caffeinate sh -c '{W} x'",
    f"firejail sh -c '{W} x'", f"parallel -j4 '{W} {{}}'", f"su --command '{W} x'",
    f"uv run {W} x", f"flock /tmp/l {W} x",
    f"trap -- '{W} x' EXIT", f"ssh h -- '{W} x'", f"bash -c -- '{W} x'",
    f"printf 'a\\n{W} x\\n' | sh", f"echo -e 'a\\n{W} x' | sh", f"echo $'a\\n{W} x' | sh",
    f"x={W}; $x y", f"x={W}; command $x y", f"bash -lc '{W} x'", f"bash -ic '{W} x'",
    "$'\\x65val' x",
]

DATA_CORPUS += [
    f"python3 - <<< '{W}'", f"cat <<'EOF' | python3 -c 'import sys; print(1)'\n{W} x\nEOF",
    f"echo {W} x | grep x | wc", f"cat f | tee g | grep {W}",
]



EXECUTING_CORPUS += [
    f"for c in {W} ls; do $c x; done", f"for c in {W} x; do sh <<< \"$c\"; done", f"for c in {W}; do bash -c \"$c x\"; done",
    f"for c in {W}; do echo $c | sh; done", f"select c in {W}; do $c; done",
    f"echo {W} x | while read c; do $c; done", f"while read c; do $c; done <<< '{W} x'",
    f"make -f <(echo 'x: ; {W} y') x", f"echo 'x: ; {W} y' | make -f - x", f"npm exec --call='{W} x'",
    f"npm exec -c '{W} x'", f"npx -c '{W} x'", f"sed '1e {W} x' f", f"sed 's/x/{W} y/e' f",
    f"git rebase --exec '{W} x' main", f"git bisect run sh -c '{W} x'", f"git submodule foreach '{W} x'",
    f"bash -o pipefail -c '{W} x'", f"bash +e -c '{W} x'", f"sh -c '$1' _ {W}", f"p='{W} x'; sh -c \"$p\"",
    f"cat <<< '{W} x' | sh", f"for i in 1; do echo '{W} x' | sh; done", f"script --command='{W} x' f",
]


EXECUTING_CORPUS += [
    f"sed '1!e {W} x' f", f"sed '$!e {W} x' f", f"sed -f - f <<< '1e {W} x'", f"for c in {{{W},ls}}; do $c x; done",
    "for c in ev{al,}; do $c x; done", f"read c <<< {W}; $c x", f"read -a a <<< {W}; ${{a[0]}} x",
    f"make -f - x <<'EOF'\nx:\n\t{W} y\nEOF", f"make --file=<(echo 'x: ; {W} y') x", f"make -sf <(echo 'x: ; {W} y') x",
    f"just -f <(echo 'x:\\n  {W} y') x", f"echo 'x:\\n  {W} y' | just -f - x",
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


# ---------------------------------------------------------------------------
# Table-driven differentials: one generated test per table, so a row cannot be
# removed unnoticed. A removed value-option row makes its value read as the
# verb, which turns the DATA command below into an ask.
# ---------------------------------------------------------------------------

from agentwire.safety import _core  # noqa: E402

# Independent spec of the launcher table: the generated tests below iterate THIS, not
# _core's table, so deleting a row from the code goes red (a test that iterated the
# code's own table would shrink with it).
EXPECTED_WRAPPER_OPTS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t", "-a", "-c", "--user", "--group",
             "--host", "--prompt", "--close-from", "--chdir", "--chroot", "--role", "--type",
             "--login-class", "--other-user"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "-P", "--unset", "--chdir", "--split-string"},
    "xargs": {"-I", "-n", "-L", "-P", "-s", "-d", "-a", "-E", "-J", "-R", "-S", "--max-args", "--max-procs",
              "--delimiter", "--arg-file", "--max-lines", "--max-chars", "--eof"},
    "timeout": {"-k", "-s", "--kill-after", "--signal"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata", "--pid"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "time": {"-f", "-o", "--format", "--output"},
    "watch": {"-n", "--interval"},
    "exec": {"-a"},
    "nohup": set(), "command": set(), "builtin": set(), "setsid": set(),
    "uv": {"--with", "--python", "-p", "--directory", "--project", "--extra", "--group", "--env-file",
           "--with-requirements", "--index", "--package", "--from"},
    "uvx": {"--from", "--with", "--python", "-p", "--with-requirements"},
    "npx": {"-p", "--package", "--cache", "--userconfig", "-c", "--call"},
    "bunx": set(), "pnpx": set(),
}
EXPECTED_TMUX_RUNNERS = ["new-session", "new", "new-window", "neww", "split-window", "splitw", "respawn-pane",
                         "respawnp", "respawn-window", "respawnw", "run-shell", "run", "if-shell", "if"]
EXPECTED_FIND_EXEC = ["-exec", "-execdir", "-ok", "-okdir"]


def test_code_tables_cover_the_spec():
    bad = [(w, sorted(set(o) - set(_core._EVAL_WRAPPER_VALUE_OPTS.get(w, ()))))
           for w, o in EXPECTED_WRAPPER_OPTS.items() if not set(o) <= set(_core._EVAL_WRAPPER_VALUE_OPTS.get(w, ()))]
    bad += [("tmux", r) for r in EXPECTED_TMUX_RUNNERS if r not in _core._EVAL_TMUX_RUNNERS]
    bad += [("find", f) for f in EXPECTED_FIND_EXEC if f not in _core._EVAL_FIND_EXEC]
    assert not bad, bad


def _opt_variants(wrapper, opt):
    """Spellings of one value-taking option: spaced, cluster-with-boolean, long ``=``."""
    if opt.startswith("--"):
        return [f"{opt} VAL", f"{opt}=VAL"]
    return [f"{opt} VAL", f"{opt}VAL"]


def test_every_wrapper_option_row_is_load_bearing():
    bad = []
    for wrapper, opts in EXPECTED_WRAPPER_OPTS.items():
        lead = "/usr/bin/time" if wrapper == "time" else wrapper  # bash's own `time` keyword takes only -p
        for opt in sorted(opts):
            for spelling in _opt_variants(wrapper, opt):
                body = f"grep {W} f"
                if wrapper == "timeout":
                    cmd = f"timeout {spelling} 5 {body}"
                elif wrapper == "time":
                    cmd = f"/usr/bin/time {spelling} {body}"
                elif (wrapper, opt) in (("env", "-S"), ("env", "--split-string"), ("npx", "-c"), ("npx", "--call")):
                    continue  # its value IS a program; covered by the payload test
                else:
                    cmd = f"{wrapper} {spelling} {body}"
                if ambiguity_conceals_verb(cmd) is not None:
                    bad.append(("data-asks", cmd))
                run = cmd.replace(body, f"{W} x")
                if ambiguity_conceals_verb(run) is None:
                    bad.append(("exec-allowed", run))
        # boolean flag / terminator keep the verb visible
        plain = f"timeout -- 5 grep {W} f" if wrapper == "timeout" else f"{lead} -- grep {W} f"
        if ambiguity_conceals_verb(plain) is not None:
            bad.append(("terminator-asks", plain))
    assert not bad, bad


def test_wrapper_bare_and_absolute_path_forms():
    bad = []
    for wrapper in EXPECTED_WRAPPER_OPTS:
        pre = "timeout 5" if wrapper == "timeout" else wrapper
        if ambiguity_conceals_verb(f"/usr/bin/{pre} grep {W} f") is not None:
            bad.append(("data", wrapper))
        if ambiguity_conceals_verb(f"/usr/bin/{pre} {W} x") is None and wrapper != "command":
            bad.append(("exec", wrapper))
    assert not bad, bad


def test_option_clusters_follow_getopt():
    cases_exec = [f"sudo -Eu root {W} x", f"sudo -Eu root sh -c '{W} x'", f"xargs -0I{{}} {W} {{}}",
                  f"sudo -Eus x {W} x", f"time -a -o f {W} x"]
    cases_data = [f"sudo -Eu root grep {W} f", f"xargs -0n1 grep {W}", f"sudo -nE grep {W} f"]
    bad = [c for c in cases_exec if ambiguity_conceals_verb(c) is None]
    bad += [c for c in cases_data if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_command_lookup_flags_are_queries():
    bad = [f"command {f} {W}" for f in ("-v", "-V", "-pv")
           if ambiguity_conceals_verb(f"command {f} {W}") is not None]
    bad += [f"command -p {W} x" for _ in [0] if ambiguity_conceals_verb(f"command -p {W} x") is None]
    assert not bad, bad


def test_shell_c_payload_forms():
    flags = ["-c", "-lc", "-ic", "-xc", "-c --"]
    shells = ["sh", "bash", "zsh", "dash", "ksh", "/bin/bash", "su", "runuser u", "script", "sg g"]
    bad = []
    for sh in shells:
        for fl in flags:
            for payload in (f"'{W} x'", f"'a; {W} x'", W):
                cmd = f"{sh} {fl} {payload}"
                if ambiguity_conceals_verb(cmd) is None:
                    bad.append(cmd)
            if ambiguity_conceals_verb(f"{sh} {fl} 'echo {W}'") is not None:
                bad.append(("data", sh, fl))
    for cmd in (f"su --command '{W} x'", f"su --command='{W} x'", f"su root -c '{W} x'",
                f"sudo bash -c '{W} x'", f"ssh h '{W} x'", f"slogin h '{W} x'", f"ssh -p 22 h -- '{W} x'"):
        if ambiguity_conceals_verb(cmd) is None:
            bad.append(cmd)
    assert not bad, bad


def test_tmux_runners_conceal_but_send_keys_is_data():
    bad = [f"tmux {r} '{W} x'" for r in EXPECTED_TMUX_RUNNERS
           if ambiguity_conceals_verb(f"tmux {r} '{W} x'") is None]
    bad += [f"tmux {r}" for r in ("send-keys -t x", "display-message", "select-pane")
            if ambiguity_conceals_verb(f"tmux {r} '{W} x'") is not None]
    assert not bad, bad


def test_find_exec_vectors():
    bad = []
    for flag in EXPECTED_FIND_EXEC:
        for term in ("\\;", "+"):
            if ambiguity_conceals_verb(f"find . {flag} {W} {{}} {term}") is None:
                bad.append(("exec", flag, term))
            if ambiguity_conceals_verb(f"find . {flag} grep -l {W} {{}} {term}") is not None:
                bad.append(("data", flag, term))
            # the executing vector is the SECOND one
            if ambiguity_conceals_verb(f"find . {flag} echo a {term} {flag} {W} x {term}") is None:
                bad.append(("second", flag, term))
    assert not bad, bad


def test_redirect_grammar_before_the_verb():
    bad = []
    prefixes = ["", "2", "10", "{fd}"]
    ops = [">", ">>", "<", "<>", ">|", "&>", "&>>", "<<<"]
    for pre in prefixes:
        if pre in ("2", "10", "{fd}") or pre == "":
            for op in ops:
                if op.startswith("&") and pre:
                    continue
                for tgt in ("f", " f"):
                    r = f"{pre}{op}{tgt}"
                    if ambiguity_conceals_verb(f"{r} {W} x") is None:
                        bad.append(("exec", r))
                    if ambiguity_conceals_verb(f"{r} grep {W} f") is not None:
                        bad.append(("data", r))
        for dup in (">&2", ">&-", "<&-", ">&1", "<&3"):
            r = f"{pre}{dup}"
            if ambiguity_conceals_verb(f"{r} {W} x") is None:
                bad.append(("exec", r))
            if ambiguity_conceals_verb(f"{r} grep {W} f") is not None:
                bad.append(("data", r))
    # several redirects, and a redirect AFTER the verb, still leave the verb a verb
    for c in (f">a 2>&1 <b {W} x", f"{W} x >out 2>&1"):
        if ambiguity_conceals_verb(c) is None:
            bad.append(c)
    assert not bad, bad


def test_unknown_launchers_ask_and_never_allow():
    launchers = ["uv run", "uv run -q", "uv run --no-project", "uvx -q", "npx -y", "bunx -b", "pnpm dlx",
                 "flock /tmp/l", "chroot /", "unshare -m", "taskset 1", "chrt 1", "strace -f", "ltrace",
                 "caffeinate", "firejail", "parallel -j4", "someunknowntool --flag"]
    bad = []
    for lead in launchers:
        for tail in (f"sh -c '{W} x'", f"bash -lc '{W} x'", f"zsh -c {W}", f"{W} x", f"'{W} {{}}'"):
            if tail.startswith("'") and lead.split()[0] in ("uv", "uvx", "npx", "bunx", "pnpm"):
                continue  # a known launcher's quoted word is a program NAME, not shell text
            cmd = f"{lead} {tail}"
            if ambiguity_conceals_verb(cmd) is None:
                bad.append(cmd)
    assert not bad, bad


def test_pipeline_walks_every_stage_to_the_shell():
    middles = ["", "tee f | ", "cat | ", "sed s/a/b/ | ", "tr a b | ", "cut -c1- | ", "head -5 | ",
               "tail -5 | ", "sort | ", "uniq | ", "awk 1 | ", "base64 -d | ", "gunzip | ",
               "openssl enc -d | ", "tee a | cat | tee b | "]
    consumers = ["sh", "bash", "zsh", "dash", "xargs sh -c", "sudo bash", "source /dev/stdin", "ssh host"]
    producers = [f"echo {W} x", f"printf '%s\\n' '{W} x'", f"echo -e 'a\\n{W} x'", f"echo $'a\\n{W} x'",
                 f"printf 'a\\n{W} x\\n'"]
    bad = []
    for pr, mid, con in itertools.product(producers, middles, consumers):
        cmd = f"{pr} | {mid}{con}"
        if ambiguity_conceals_verb(cmd) is None:
            bad.append(cmd)
    for mid, con in itertools.product([m for m in middles if "base64" not in m], ["python3", "node", "ruby", "perl", "python3 -c 'print(1)'"]):
        cmd = f"echo {W} x | {mid}{con}"
        if ambiguity_conceals_verb(cmd) is not None:
            bad.append(("interp", cmd))
    # a data producer feeding a shell is not eval
    for cmd in ("echo hi | tee f | sh", "printf 'a\\nb' | bash"):
        if ambiguity_conceals_verb(cmd) is not None:
            bad.append(("data", cmd))
    assert not bad, bad


def test_process_substitution_operands():
    shells = ["bash", "sh", "zsh", "dash", "ksh", "source", ".", "sudo bash", "bash -x", "bash <"]
    bodies = [f"echo {W} x", f"echo a; echo {W} x", f"printf '%s' '{W} x'", "echo a | tee f"]
    bad = []
    for sh, body in itertools.product(shells, bodies):
        cmd = f"{sh} <({body})"
        want = W in body
        if (ambiguity_conceals_verb(cmd) is not None) != want:
            bad.append((cmd, want))
    for cmd in (f"diff <(echo {W}) <(echo b)", f"cat <(grep {W} f)"):
        if ambiguity_conceals_verb(cmd) is not None:
            bad.append(cmd)
    assert ambiguity_conceals_verb(f"cat <({W} x)") is not None
    assert not bad, bad


def test_variable_alias_of_the_builtin():
    bad = [c for c in (f"x={W}; $x y", f"x={W}; command $x y", f'x="{W} y"; $x', f"x='{W}'; ${{x}} y")
           if ambiguity_conceals_verb(c) is None]
    bad += [c for c in (f"x={W}; echo $x", "x=ls; $x y", f"x={W}x; $x y")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_dollar_quote_spellings():
    bad = [c for c in ("$'\\x65val' x", "$'ev''al' x", f'$"{W}" x', "$'e\\x76al' x")
           if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


def test_nesting_beyond_the_depth_budget_fails_closed():
    cmd = f"{W} x"
    for _ in range(_core._RESCAN_MAX_DEPTH + 3):
        cmd = f"sh -c {shlex.quote(cmd)}"
    assert ambiguity_conceals_verb(cmd) is not None
    shallow = f"sh -c {shlex.quote(f'echo {W}')}"
    assert ambiguity_conceals_verb(shallow) is None


def test_interpreter_stdin_is_not_shell_text(cfg):
    for c in (f"python3 - <<< '{W}'", f"cat <<'EOF' | python3 -c 'print(1)'\n{W} x\nEOF",
              f"python3 <<'EOF'\n{W}(x)\nEOF"):
        assert ambiguity_conceals_verb(c) is None, c
        assert check_command(c, cfg)["decision"] == "allow", c


def test_payloads_behind_unknown_launchers_use_the_full_rescan():
    """(a) of the unknown rule rescans a shell payload with the unknown rule still ON, so a
    launcher nested INSIDE the payload cannot hide the builtin; the quoted-string guess cannot."""
    bad = [c for c in (f"flock f sh -c 'uv run {W} x'", f"uvx -q bash -c 'flock f {W} x'",
                       f"env --split-string '{W} x'", f"env --split-string='{W} x'", f"env -S'{W} x'")
           if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


def test_terminators_and_quoted_prose_after_known_and_unknown_commands():
    bad = [c for c in (f"ssh h -- grep {W} f", f"slogin h grep {W} f", f"ssh -p 22 h grep {W} f",
                       f"trap -- 'echo {W}' EXIT", f"someunknowntool 'note: {W} is bad'",
                       f"someunknowntool --flag 'a {W} b'", f"find . -exec grep a {{}} + -name {W}")
           if ambiguity_conceals_verb(c) is not None]
    bad += [c for c in (f"ssh h -- '{W} x'", f"ssh -p 22 h -- {W} x", f"trap -- '{W} x' EXIT",
                        f"find . -exec ls {{}} + -exec {W} x +")
            if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


# ---------------------------------------------------------------------------
# Round 3: the shell's own option grammar, positional/variable payloads, producers
# inside compound bodies, and the wrongful blocks the inverted direction introduced.
# ---------------------------------------------------------------------------

SHELL_PREFIXES = [
    "-c", "-o pipefail -c", "+e -c", "-O extglob -c", "+O extglob -c", "+o nounset -c", "--rcfile f -c",
    "--init-file f -c", "--norc --noprofile --posix -c", "-e -u -o pipefail -c", "-ec", "-xc", "-lc", "-ic",
    "-eo pipefail -c", "-c -o pipefail", "-c --", "-e -c --", "-e -o pipefail -c --",
]


def test_shell_option_grammar_before_the_payload():
    bad = []
    for sh in ("bash", "sh", "zsh", "dash", "ksh", "/bin/bash"):
        for pre in SHELL_PREFIXES:
            if ambiguity_conceals_verb(f"{sh} {pre} '{W} x'") is None:
                bad.append(("exec", sh, pre))
            if ambiguity_conceals_verb(f"{sh} {pre} 'echo {W}'") is not None:
                bad.append(("data", sh, pre))
    # no -c: the first non-option word is a script file; what follows is its arguments
    for c in (f"bash -o pipefail script.sh {W}", f"bash -e script.sh -c {W}", f"bash --norc script.sh {W}"):
        if ambiguity_conceals_verb(c) is not None:
            bad.append(c)
    assert not bad, bad


def test_positional_parameters_as_command_text():
    bad = [c for c in (f"bash -c '$0 x' {W}", f"sh -c '$1' _ {W}", f"sh -c '\"$@\"' _ {W} x",
                       f"sh -c 'exec \"$@\"' _ {W} x", f"sh -c 'command $1 y' _ {W}",
                       f"sh -c '$1 y' _ 'x; {W} z'")
           if ambiguity_conceals_verb(c) is None]
    bad += [c for c in (f"sh -c 'echo \"$1\"' _ {W}", f"sh -c 'ls $1' _ {W}", f"bash -c 'cat \"$@\"' _ {W}")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_variable_payloads():
    bad = [c for c in (f"p='{W} x'; sh -c \"$p\"", f"x={W}; bash -c \"$x y\"", f"x={W}; bash -c 'echo a; $x y'",
                       f"p=\"{W} x\"; sh -o pipefail -c \"$p\"")
           if ambiguity_conceals_verb(c) is None]
    bad += [c for c in ("x=ls; sh -c \"$x y\"", f"p='echo {W}'; echo \"$p\"")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_producers_inside_compound_bodies_and_here_strings():
    producers = [f"cat <<< '{W} x'", f"tee f <<< '{W} x'", f"echo '{W} x'", f"printf '%s' '{W} x'",
                 f"echo '\\{W} x'", f"echo -e 'a\\n{W} x'"]
    bodies = ["{c}", "for i in 1; do {c}; done", "while true; do {c}; done", "if true; then {c}; fi",
              "case a in a) {c} ;; esac", "f() {{ {c}; }}; f", "( {c} )", "{{ {c}; }}",
              "if x; then :; else {c}; fi"]
    bad = []
    for pr, body in itertools.product(producers, bodies):
        cmd = body.format(c=f"{pr} | sh")
        if ambiguity_conceals_verb(cmd) is None:
            bad.append(cmd)
    for body in bodies:
        for cmd in (body.format(c="echo hello | sh"), body.format(c=f"echo '{W}' | grep x")):
            if ambiguity_conceals_verb(cmd) is not None:
                bad.append(("data", cmd))
    assert not bad, bad


def test_su_style_runners_own_c_options():
    bad = [c for c in (f"script --command='{W} x' f", f"script -c '{W} x' f", f"script --command '{W} x'",
                       f"runuser -u u -- sh -c '{W} x'", f"runuser -l u -c '{W} x'", f"sg grp -c '{W} x'",
                       f"su root -c '{W} x'", f"su -l root --command='{W} x'")
           if ambiguity_conceals_verb(c) is None]
    bad += [c for c in (f"script -c 'echo {W}' f", f"su root -c 'ls {W}'")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_word_lists_and_quoted_values_are_data():
    bad = [c for c in (f"for f in a {W}; do echo $f; done", f"for f in {W} b; do :; done",
                       f"select x in {W} b; do :; done", f"case {W} in a) :;; esac",
                       f"say '{W}'", f"uv run pytest -k '{W} or exec'", f"uv run -q grep {W} f",
                       f"uv run --with x python -c 'print(1)' {W}", f"uvx --from pkg ruff {W}",
                       f"npx -y grep {W}", f"npx -p pkg ruff {W}")
           if ambiguity_conceals_verb(c) is not None]
    bad += [c for c in (f"for i in 1; do {W} \"$i\"; done", f"uv run {W} x", f"uv run -q sh -c '{W} x'",
                        f"uvx -p 3.12 {W} x", f"npx -c '{W} x'", f"npx --call '{W} x'", f"uv run --with x {W} y")
            if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


# Realistic harmless commands, each with its expected verdict. "allow" must stay allowed; every
# "ask" is a DECLARED wrongful ask with its reason, so the cost of the inverted failure direction
# is a reviewed list, not a surprise.
HARMLESS_CORPUS = [
    ("git status", "allow"), (f"npx jest -t {W}", "allow"), (f"pnpm exec vitest -t {W}", "allow"), (f"poetry run pytest -k {W}", "allow"), (f"dotnet test --filter {W}", "allow"), (f"docker build -t {W} .", "allow"), (f"kubectl get pods {W}", "allow"), (f"helm template {W} ./chart", "allow"), (f"sudo systemctl restart {W}", "allow"), (f"brew install {W}", "allow"), (f"uv add {W}", "allow"), ("git diff --stat origin/main", "allow"), ("git log --oneline -5", "allow"),
    ("git commit -m 'fix the loop'", "allow"), (f"git commit -m 'damage-control: {W} by position'", "allow"),
    (f"gh issue create --title t --body 'the {W} builtin asks'", "allow"),
    (f"gh pr create --draft --body 'Closes #1115. {W} no longer asks on prose'", "allow"),
    (f"gh issue comment 1115 --body 'repro: {W} \"$x\"'", "allow"),
    (f"agentwire msg send --to x --kind done 'fixed the {W} rule'", "allow"),
    (f"agentwire send -s x 'the {W} ask'", "allow"),
    (f"grep -rn '{W}' agentwire/safety", "allow"), (f"grep -rnE '(^| ){W}( |$)' agentwire", "allow"),
    (f"rg {W} agentwire", "allow"), (f"rg -n '{W}' --glob '*.py'", "allow"), (f"grep -n {W} docs/wiki/internals/damage-control.md | head", "allow"),
    (f"cat docs/{W}.md", "allow"), (f"ls docs/wiki | grep {W}", "allow"), (f"wc -l tests/unit/test_{W}.py", "allow"),
    (f"echo 'remember the {W} case'", "allow"), (f"printf '%s\\n' '{W}'", "allow"), (f"echo {W} > notes.txt", "allow"),
    (f"man {W}", "allow"), (f"type {W}", "allow"), (f"command -v {W}", "allow"), (f"help {W}", "allow"),
    (f"cat > notes.md <<'EOF'\nwe discussed {W} \"$v\" today\nEOF", "allow"),
    (f"python3 - <<'EOF'\nprint('{W}')\nEOF", "allow"), (f"python3 -c \"print('{W}')\"", "allow"),
    (f"uv run pytest tests -k '{W}'", "allow"), (f"uv run pytest -k '{W} or exec' -q", "allow"),
    ("uv run -q ruff check agentwire", "allow"), ("uvx ruff check agentwire", "allow"),
    ("npx -y prettier --check .", "allow"), ("npm run build", "allow"),
    (f"sed -n '/{W}/p' notes.txt", "allow"), (f"awk '/{W}/ {{print $1}}' notes.txt", "allow"),
    (f"find . -name '*{W}*'", "allow"), (rf"find . -exec grep -l {W} {{}} \;", "allow"),
    (f"xargs -I{{}} grep {W} {{}}", "allow"), (f"sudo -E grep {W} /var/log/x", "allow"),
    (f"env -i grep {W} f", "allow"), (f"nohup -- grep {W} f", "allow"), (f"watch echo {W}", "allow"),
    (f"tmux send-keys -t x '{W} foo' Enter", "allow"),
    (f"for f in a {W}; do echo $f; done", "allow"), (f"say '{W}'", "allow"),
    (f"bash -o pipefail -c 'echo {W}'", "allow"), (f"bash script.sh {W}", "allow"),
    (f"sh -c 'echo \"$1\"' _ {W}", "allow"), (f"echo {W} | grep x | wc -l", "allow"),
    (f"cat f | tee g | grep {W}", "allow"), ("echo hi | tee f | sh", "allow"),
    (f"echo {W} | python3 -c 'import sys; print(sys.stdin.read())'", "allow"),
    ("ls -la && pwd", "allow"), ("mkdir -p build && cd build", "allow"), ("make test", "allow"),
    ("cargo build --release", "allow"), ("pip list | grep requests", "allow"), ("docker ps -a", "allow"),
    ("curl -s https://example.com | jq .", "allow"), ("tar -czf a.tgz dir", "allow"), ("diff a b", "allow"),
    # declared wrongful asks: an unknown launcher followed by the bare word cannot be told from a verb
    (f"someunknowntool {W}", "ask"),
    # launchers with a modelled grammar (#14): the word after the lock file / before ``:::`` is the verb
    (f"parallel -j4 grep {W} ::: f", "allow"), (f"flock /tmp/l grep {W} f", "allow"),
    (f"foo --flag '{W}'", "ask"),
]


def test_realistic_harmless_corpus_matches_its_expected_verdicts(cfg):
    bad = []
    for cmd, want in HARMLESS_CORPUS:
        got = "allow" if check_command(cmd, cfg)["decision"] == "allow" else "ask"
        if got != want:
            bad.append((cmd, want, got))
    assert not bad, bad
    declared = [c for c, w in HARMLESS_CORPUS if w == "ask"]
    assert len(declared) <= 6, declared  # the price of failing toward ask stays a short, reviewed list


# ---------------------------------------------------------------------------
# Final round: tables pinned from independent spec copies, more variable shapes,
# project runners, pipeline sequencing.
# ---------------------------------------------------------------------------

EXPECTED_DATA_CONSUMERS = ["mapfile", "readarray", "jest", "vitest", "mocha", "tsc", "eslint", "prettier", "dotnet", '[', 'ack', 'ag', 'agentwire', 'awk', 'base64', 'basename', 'bat', 'bun', 'cat', 'cd', 'chmod', 'chown', 'cmp', 'column', 'comm', 'cp', 'curl', 'cut', 'date', 'deno', 'df', 'diff', 'dirname', 'du', 'echo', 'egrep', 'export', 'false', 'fgrep', 'file', 'fmt', 'fold', 'gawk', 'gh', 'git', 'grep', 'gunzip', 'gzip', 'head', 'help', 'hexdump', 'info', 'join', 'jq', 'kill', 'less', 'ln', 'ls', 'lsof', 'man', 'md5', 'mkdir', 'more', 'mv', 'nl', 'node', 'od', 'open', 'openssl', 'paste', 'pbcopy', 'perl', 'pgrep', 'php', 'pip', 'pkill', 'printf', 'ps', 'pwd', 'pytest', 'python', 'python2', 'python3', 'read', 'readlink', 'realpath', 'rg', 'rm', 'rsync', 'ruby', 'ruff', 'say', 'scp', 'sed', 'shasum', 'sleep', 'sort', 'split', 'ssh-keygen', 'stat', 'strings', 'tail', 'tar', 'tee', 'test', 'touch', 'tr', 'tree', 'true', 'type', 'uniq', 'unset', 'unzip', 'wc', 'wget', 'whatis', 'whence', 'whereis', 'which', 'xxd', 'yq', 'zgrep', 'zip']
EXPECTED_INTERPRETERS = ["perl", "python", "python2", "python3", "node", "ruby", "php", "deno", "bun"]
EXPECTED_SHELLS = ["sh", "bash", "zsh", "dash", "ksh", "su", "ash", "fish", "csh", "tcsh", "source", "."]
EXPECTED_PROJECT_RUNNERS = ["make", "just", "cargo", "npm", "pnpm", "yarn", "fd", "go", "bundle"]


def test_tables_contain_their_spec():
    bad = [("data", w) for w in EXPECTED_DATA_CONSUMERS if w not in _core._EVAL_DATA_CONSUMERS]
    bad += [("interp", w) for w in EXPECTED_INTERPRETERS if w not in _core._EVAL_INTERPRETERS]
    bad += [("shell", w) for w in EXPECTED_SHELLS if w not in _core._EVAL_SHELLS]
    bad += [("runner", w) for w in EXPECTED_PROJECT_RUNNERS if w not in _core._EVAL_PROJECT_RUNNERS]
    assert not bad, bad


def test_every_data_consumer_keeps_the_word_as_data():
    bad = [w for w in EXPECTED_DATA_CONSUMERS if ambiguity_conceals_verb(f"{w} {W} f") is not None]
    bad += [f"/usr/bin/{w}" for w in EXPECTED_DATA_CONSUMERS
            if ambiguity_conceals_verb(f"/usr/bin/{w} -x {W}") is not None]
    assert not bad, bad


def test_every_interpreter_fed_a_body_is_not_shell_text():
    bad = []
    for i in EXPECTED_INTERPRETERS:
        for cmd in (f"echo {W} x | {i}", f"echo {W} x | sudo {i}", f"cat <<'EOF' | {i}\n{W} x\nEOF",
                    f"{i} <<< '{W} x'", f"echo {W} x | tee f | {i} -"):
            if ambiguity_conceals_verb(cmd) is not None:
                bad.append(cmd)
    assert not bad, bad


def test_every_shell_fed_a_body_conceals():
    bad = []
    for sh in EXPECTED_SHELLS:
        if sh in ("source", "."):
            cmds = [f"echo {W} x | {sh} /dev/stdin", f"{sh} <(echo {W} x)"]
        else:
            cmds = [f"echo {W} x | {sh}", f"echo {W} x | sudo {sh}", f"cat <<'EOF' | {sh}\n{W} x\nEOF",
                    f"{sh} <<< '{W} x'", f"{sh} <(echo {W} x)"]
        bad += [c for c in cmds if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


def test_only_piped_producers_feed_the_shell():
    bad = [c for c in (f"echo {W} x; echo hi | sh", f"echo {W} x && echo hi | sh", f"echo {W} x\necho hi | sh")
           if ambiguity_conceals_verb(c) is not None]
    bad += [c for c in ("echo hi | { sh; }", ) if ambiguity_conceals_verb(c) is not None]
    bad += [c for c in (f"echo {W} x | {{ sh; }}", f"echo {W} x | (sh)", f"echo {W} x | if sh")
            if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


def test_more_variable_shapes():
    exec_ = [f"p='{W} x'; echo \"$p\" | sh", f"p='{W} x'; sh <<< \"$p\"", f"p='{W} x' sh -c '$p'",
             f"x={W}; y=$x; $y z", f"x={W}; y=\"$x\"; $y z", f"${{x:-{W}}} y", "echo 'ev\\al x' | sh",
             f"bash -c \\\n'{W} x'", f"x={W}; echo \"$x y\" | bash"]
    data = ["p=ls; echo \"$p\" | sh", "x=ls; y=$x; $y z", f"p='{W}'; echo \"$p\""]
    bad = [c for c in exec_ if ambiguity_conceals_verb(c) is None]
    bad += [c for c in data if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_project_runners_take_the_word_as_a_target_or_selector():
    data = [f"make {W}", f"just {W}", f"cargo test {W}", f"npm run {W}", f"pnpm test -- -t {W}",
            f"yarn test --grep {W}", f"fd {W}", f"go test -run {W} ./...", f"uv run pytest -k {W}",
            f"bundle exec rspec -e {W}".replace("exec ", "")]
    ask = [f"make SHELL=/bin/sh {W}", f"make .SHELLFLAGS=-c {W}", f"npm exec {W} x", f"pnpm dlx {W}",
           f"yarn exec {W} x", f"npm exec -- sh -c '{W} x'", f"pnpm exec sh -c '{W} x'",
           f"yarn exec bash -c '{W} x'", f"uv run sh -c '{W} x'", f"uv run bash -c '{W} x'",
           f"cargo run -- sh -c '{W} x'", f"make -f m.mk SHELL=./wrapper {W}"]
    bad = [c for c in data if ambiguity_conceals_verb(c) is not None]
    bad += [c for c in ask if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


def test_shell_options_after_dash_dash_are_a_script_not_a_payload():
    assert ambiguity_conceals_verb(f"bash -- -c {W}") is None
    assert ambiguity_conceals_verb(f"bash -e -- script.sh -c {W}") is None


def test_unparsed_option_shapes_fall_back_to_scanning_for_c():
    bad = [c for c in (f"bash -O3 -c '{W} x'", f"bash -O3 -x -lc '{W} x'", f"bash - -c '{W} x'")
           if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


# ---------------------------------------------------------------------------
# Round 4: loop variables, makefile programs, npm --call, sed's executing forms,
# git payload subcommands, and the package/service/test-filter data forms.
# ---------------------------------------------------------------------------

def test_loop_variable_carries_the_word_into_the_body():
    use = ["$c x", "${c} x", "\"$c\" x", "sh <<< \"$c\"", "bash -c \"$c x\"", "echo $c | sh", "echo \"$c\" | bash",
           "command $c x", "sudo $c x"]
    bad = []
    for kw in ("for", "select"):
        for body in use:
            for lst in (W, f"{W} ls", f"ls {W}", f"'{W}' x", "e''val x"):
                cmd = f"{kw} c in {lst}; do {body}; done"
                if ambiguity_conceals_verb(cmd) is None:
                    bad.append(cmd)
    for cmd in (f"echo {W} x | while read c; do $c; done", f"while read c; do $c; done <<< '{W} x'",
                f"while read -r a b; do $a $b; done < <(echo {W} x)", f"echo {W} | while IFS= read line; do sh -c \"$line\"; done"):
        if ambiguity_conceals_verb(cmd) is None:
            bad.append(cmd)
    # the list is DATA while the variable stays out of command position
    for cmd in (f"for f in a {W}; do echo $f; done", f"for f in {W}; do ls \"$f\"; done",
                f"for f in {W}; do grep -n $f x; done", "echo hi | while read c; do echo $c; done",
                "for f in a b; do $f x; done", "echo hi | while read c; do $c; done"):
        if ambiguity_conceals_verb(cmd) is not None:
            bad.append(("data", cmd))
    assert not bad, bad


def test_makefile_programs_are_shell_text():
    bad = [c for c in (f"make -f <(echo 'x: ; {W} y') x", f"echo 'x: ; {W} y' | make -f - x",
                       f"make -f - x <<< 'x: ; {W} y'", f"make -f /dev/stdin <<< $'x:\\n\\t{W} y'",
                       f"gmake -f <(printf 'x: ; {W} y') x", f"cat <<'EOF' | make -f -\nx:\n\t{W} y\nEOF")
           if ambiguity_conceals_verb(c) is None]
    bad += [c for c in ("make -f <(echo 'x: ; echo hi') x", f"make -f Makefile {W}", "echo hi | make -f - x")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_npm_family_call_options_and_exec_payloads():
    bad = [c for c in (f"npm exec --call='{W} x'", f"npm exec --call '{W} x'", f"npm exec -c '{W} x'",
                       f"npx -c '{W} x'", f"pnpm exec sh -c '{W} x'", f"yarn exec bash -c '{W} x'",
                       f"npm exec -- sh -c '{W} x'", f"pnpm dlx {W} x", f"npm exec {W} x")
           if ambiguity_conceals_verb(c) is None]
    bad += [c for c in (f"npx jest -t {W}", f"pnpm exec vitest -t {W}", f"npm exec -- jest -t {W}",
                        f"npm run {W}", f"npm install {W}")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_sed_executing_forms():
    bad = [c for c in (f"sed '1e {W} x' f", f"sed -e '1e {W} x' f", f"sed -n '/p/e {W} z' f",
                       f"sed '$e {W} x' f", f"sed '2{{e {W} x\n}}' f", f"sed 's/x/{W} y/e' f",
                       f"sed 's|x|{W} y|e' f", f"sed -e 's/x/{W} y/ge' f", f"gsed '1e {W} x' f")
           if ambiguity_conceals_verb(c) is None]
    bad += [c for c in (f"sed -n '/{W}/p' f", f"sed 's/{W}/x/' f", f"sed 's/a/{W}/g' f", "sed -e '1d' f",
                        f"sed 's/{W}/y/e' f".replace(f"s/{W}/y/e", f"s/{W}/y/g"), f"sed '/{W}/d' f",
                        f"sed 's/e/{W}/' f", "sed '1e echo hi' f", "sed 's/a/b/e' f")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_git_subcommands_that_run_a_command_line():
    bad = [c for c in (f"git rebase --exec '{W} x' main", f"git rebase -x '{W} x'", f"git rebase --exec='{W} x'",
                       f"git bisect run sh -c '{W} x'", f"git bisect run {W} x", f"git submodule foreach '{W} x'",
                       f"git submodule foreach --recursive '{W} x'")
           if ambiguity_conceals_verb(c) is None]
    bad += [c for c in ("git rebase --exec 'make test' main", f"git bisect run make {W}", f"git log --grep {W}",
                        f"git commit -m '{W}'", "git submodule foreach 'git pull'")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_package_service_and_test_filter_verbs_take_the_word_as_a_name():
    data = [f"uv add {W}", f"uv sync --extra {W}", f"brew install {W}", f"pip install {W}", f"pip3 install {W}",
            f"sudo systemctl restart {W}", f"systemctl status {W}", f"kubectl get pods {W}", f"kubectl describe pod {W}",
            f"helm template {W} ./chart", f"docker build -t {W} .", f"docker logs {W}", f"docker ps --filter name={W}",
            f"npx jest -t {W}", f"pnpm exec vitest -t {W}", f"poetry run pytest -k {W}", f"dotnet test --filter {W}",
            f"npm install {W}"]
    ask = [f"docker run img sh -c '{W} x'", f"docker exec c sh -c '{W} x'", f"kubectl exec p -- sh -c '{W} x'",
           f"poetry run sh -c '{W} x'", f"uv run bash -c '{W} x'", f"docker run img {W} x", f"kubectl exec p -- {W} x",
           f"sudo systemctl restart {W}".replace("restart", "edit") if False else f"docker compose run svc sh -c '{W} x'"]
    bad = [c for c in data if ambiguity_conceals_verb(c) is not None]
    bad += [c for c in ask if ambiguity_conceals_verb(c) is None]
    assert not bad, bad


# Declared: a data consumer that can itself run a command line is not modelled except sed's
# `e`/`s///e` and git's rebase --exec / bisect run / submodule foreach. These still allow.
UNMODELLED_EXEC_SHAPES = [
    f"awk 'BEGIN{{system(\"{W} x\")}}'", f"python3 -c 'import os; os.system(\"{W} x\")'",
    f"perl -e 'system(\"{W} x\")'", f"vim -c '!{W} x' f", f"tar --to-command='{W} x' -xf a.tar",
    f"rsync -e '{W} x' a b",
]


def test_unmodelled_data_consumer_exec_shapes_are_declared_not_claimed():
    still_allowed = [c for c in UNMODELLED_EXEC_SHAPES if ambiguity_conceals_verb(c) is None]
    assert still_allowed == UNMODELLED_EXEC_SHAPES, "a shape became modelled: move it out of the declared list"


# ---------------------------------------------------------------------------
# Round 5: sed address forms, brace-list loops, read-family producers, make/just
# file-option grammar.
# ---------------------------------------------------------------------------

def test_sed_e_command_after_any_address_form():
    addrs = ["1", "$", "/a/", "1,3", "1!", "$!", "/a/!", "1,3!", "1 ! ", "/a/,/b/!", "2,$!", ""]
    bad = []
    for a in addrs:
        for script in (f"{a}e {W} x", f"{{{a}e {W} x\n}}", f"p;{a}e {W} x", f"1d\n{a}e {W} x"):
            for form in (f"sed '{script}' f", f"sed -n -e '{script}' f", f"sed --expression='{script}' f",
                         f"gsed -e '{script}' f", f"sed -e'{script}' f"):
                if ambiguity_conceals_verb(form) is None:
                    bad.append(form)
    for form in (f"sed -f - f <<< '1!e {W} x'", f"echo '1e {W} x' | sed -f - f", f"sed -f /dev/stdin f <<'EOF'\n1e {W} x\nEOF",
                 f"printf '1e {W} x' | sed --file=- f", f"sed -f- f <<< '1e {W} x'", f"sed -f/dev/stdin f <<< '1e {W} x'",
                 f"echo '1e {W} x' | sed -f/dev/fd/0 f"):
        if ambiguity_conceals_verb(form) is None:
            bad.append(form)
    for form in ("sed '1!e echo hi' f", f"sed '1!d;/{W}/p' f", f"sed -n '/{W}/!p' f", "sed -f s.sed f",
                 "sed -f - f <<< 's/a/b/'", f"sed 's/e/{W}/' f", "sed '1e' f".replace("1e", "1d")):
        if ambiguity_conceals_verb(form) is not None:
            bad.append(("data", form))
    assert not bad, bad


def test_brace_expanded_loop_lists():
    lists = [f"{{{W},ls}}", f"{{ls,{W}}}", "e{val,x}", "ev{al,}", f"{{a,b}}{{x,{W}}}".replace("{a,b}", ""),
             f"{{{W},}}", f"x {{ls,{W}}} y"]
    bad = [f"for c in {lst}; do $c x; done" for lst in lists
           if ambiguity_conceals_verb(f"for c in {lst}; do $c x; done") is None]
    bad += ["for c in {a,b}; do $c x; done" for _ in [0]
            if ambiguity_conceals_verb("for c in {a,b}; do $c x; done") is not None]
    bad += [("data", c) for c in (f"for c in {{a,{W}}}; do echo $c; done", "for c in {1..3}; do $c x; done",
                                  "for c in {ls,pwd}; do $c; done")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad
    big = ",".join(f"v{i}" for i in range(400))
    assert ambiguity_conceals_verb(f"for c in {{{big}}}; do $c x; done") is not None  # over the bound: asks
    product = "{a,b}" * 9  # 512 combinations: the product, not one group, crosses the bound
    assert ambiguity_conceals_verb(f"for c in {product}; do $c x; done") is not None


def test_read_family_makes_its_variables_aliases():
    exec_ = [f"read c <<< {W}; $c x", f"while read c <<< {W}; do $c x; done", f"read -r c <<< {W}; sudo $c x",
             f"read c <<< '{W} x'; bash -c \"$c\"", f"mapfile a <<< {W}; ${{a[0]}} x", f"read -a a <<< {W}; ${{a[0]}} x",
             f"readarray -t a <<< {W}; \"${{a[0]}}\" x", f"read <<< {W}; $REPLY x", f"mapfile <<< {W}; ${{MAPFILE[0]}} x",
             f"echo {W} | read c; $c x", f"read -r -p 'go: ' c <<< {W}; $c"]
    data = ["read c <<< hi; $c x", f"read c <<< {W}; echo $c", f"read c <<< {W}; ls \"$c\"", "mapfile a < f; ${a[0]} x"]
    bad = [c for c in exec_ if ambiguity_conceals_verb(c) is None]
    bad += [("data", c) for c in data if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad


def test_make_and_just_file_option_grammar():
    stdin_forms = ["-f -", "-f- ", "-f /dev/stdin", "-f /dev/fd/0", "--file=-", "--file -", "--makefile=-", "--makefile -",
                   "-sf -", "-ksf -", "-f/dev/stdin"]
    bad = []
    for form in stdin_forms:
        for tail in (f"x <<'EOF'\nx:\n\t{W} y\nEOF", f"x <<EOF\nx:\n\t{W} y\nEOF", f"x <<< $'x:\\n\\t{W} y'"):
            cmd = f"make {form} {tail}"
            if ambiguity_conceals_verb(cmd) is None:
                bad.append(cmd)
        if ambiguity_conceals_verb(f"echo 'x: ; {W} y' | make {form} x") is None:
            bad.append(f"echo | make {form}")
    for form in ("-f <(", "--file=<(", "--makefile <(", "-sf <(", "-f<(", "--file <("):
        cmd = f"make {form}echo 'x: ; {W} y') x"
        if ambiguity_conceals_verb(cmd) is None:
            bad.append(cmd)
    for cmd in (f"just --justfile <(echo 'x:\\n  {W} y') x", f"echo 'x:\\n  {W} y' | just -f - x",
                f"just -f <(echo 'x:\\n  {W} y') x", f"just -f- x <<'EOF'\nx:\n  {W} y\nEOF",
                f"just --justfile=/dev/stdin x <<< $'x:\\n  {W} y'", f"just --justfile - x <<'EOF'\nx:\n  {W} y\nEOF"):
        if ambiguity_conceals_verb(cmd) is None:
            bad.append(cmd)
    for cmd in (f"make -f Makefile {W}", f"make --file=Makefile {W}", f"just --justfile j {W}", f"make -sf Makefile {W}",
                "make -f - x <<'EOF'\nx:\n\techo hi\nEOF", "make -f <(echo 'x: ; echo hi') x", f"just -f j.just {W}",
                f"make -C dir -f Makefile {W}"):
        if ambiguity_conceals_verb(cmd) is not None:
            bad.append(("data", cmd))
    assert not bad, bad


# Declared (docs): shapes that run a program but are not modelled. Pinned as still allowed so the
# documented residual list cannot drift from the code.
DECLARED_UNMODELLED_R5 = [
    f"npm exec -c='{W} x'", f"npm exec -c{W}", f"git rebase -x{W} main", f"git difftool --extcmd='{W} x'",
    f"git difftool -x '{W} x'", f"git fetch --upload-pack='{W} x' origin", f"git config core.editor '{W} x'",
    f"GIT_SEQUENCE_EDITOR='{W} x' git rebase -i main".replace(f"GIT_SEQUENCE_EDITOR='{W} x' ", "GIT_SEQUENCE_EDITOR=true "),
]


def test_declared_unmodelled_shapes_stay_declared():
    modelled = [c for c in DECLARED_UNMODELLED_R5 if ambiguity_conceals_verb(c) is not None]
    assert not modelled, ("a declared residual became modelled; update the docs and this list", modelled)


EXPECTED_DATA_SUBCOMMANDS = {'uv': ['add', 'build', 'init', 'lock', 'pip', 'publish', 'remove', 'sync', 'tree'], 'docker': ['build', 'images', 'inspect', 'logs', 'ps', 'pull', 'push', 'rm', 'rmi', 'stop', 'tag'], 'kubectl': ['apply', 'config', 'delete', 'describe', 'get', 'logs', 'rollout', 'top'], 'helm': ['install', 'lint', 'list', 'repo', 'show', 'template', 'uninstall', 'upgrade'], 'brew': ['info', 'install', 'list', 'reinstall', 'search', 'uninstall', 'update', 'upgrade'], 'pip': ['download', 'freeze', 'install', 'list', 'show', 'uninstall'], 'pip3': ['download', 'freeze', 'install', 'list', 'show', 'uninstall'], 'systemctl': ['disable', 'enable', 'is-active', 'reload', 'restart', 'show', 'start', 'status', 'stop'], 'journalctl': []}


def test_data_subcommand_rows_are_all_present_and_all_data():
    missing = [(h, sub) for h, subs in EXPECTED_DATA_SUBCOMMANDS.items() for sub in subs
               if sub not in _core._EVAL_DATA_SUBCOMMANDS.get(h, ())]
    bad = [f"{h} {sub} {W}" for h, subs in EXPECTED_DATA_SUBCOMMANDS.items() for sub in subs
           if ambiguity_conceals_verb(f"{h} {sub} {W}") is not None]
    bad += [f"sudo {h} {sub} -x {W}" for h, subs in EXPECTED_DATA_SUBCOMMANDS.items() for sub in subs
            if ambiguity_conceals_verb(f"sudo {h} {sub} -x {W}") is not None]
    assert not missing and not bad, (missing, bad)


def test_wrapper_subcommands_launch_their_program():
    bad = [f"{h} run grep {W} f" for h in ("uv", "poetry") if ambiguity_conceals_verb(f"{h} run grep {W} f") is not None]
    bad += [f"{h} run {W} x" for h in ("uv", "poetry") if ambiguity_conceals_verb(f"{h} run {W} x") is None]
    assert not bad, bad


def test_sed_address_flags_and_custom_delimiters():
    bad = [c for c in (f"sed '/a/I!e {W} x' f", f"sed '/a/M!e {W} x' f", f"sed '/a/I e {W} x' f", f"sed '\\%a%!e {W} x' f",
                       f"sed '\\,a,e {W} x' f", f"sed '\\|a|!e {W} x' f", f"sed '/a/,/b/I!e {W} x' f")
           if ambiguity_conceals_verb(c) is None]
    bad += [c for c in ("sed '/a/I!d' f", "sed '\\%a%!d' f", f"sed '/a/I!p;/{W}/p' f")
            if ambiguity_conceals_verb(c) is not None]
    assert not bad, bad
