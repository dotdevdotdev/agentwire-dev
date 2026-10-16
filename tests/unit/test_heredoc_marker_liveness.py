"""A heredoc marker is live only when bash reads it as a redirection (#1106).

A ``<<'EOF'`` inside a comment, after a backslash, in arithmetic, in ``[[ ]]``
or as a ``let`` argument opens no heredoc. Treating it as one hid every later
line from the rules, so any dangerous command after a comment voided the whole
ladder. These tests pin the OPERATION: a canary is a marker context bash does
not read as a heredoc followed by a dangerous line; a control is a real heredoc
whose prose body must stay hidden (the false positives #1097/#1102 removed).

Loads the bundled rules, never the live ``~/.agentwire/damage-control`` copies.
"""

from pathlib import Path

import pytest
import yaml

from agentwire.safety._core import _heredocs, check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"

PLAIN = "cd ~/.agentwire && touch damagecontrol.yml"
TAILS = [
    "rm -rf /",
    PLAIN,
    "echo x >| ~/.agentwire/damagecontrol.yml",
    "git -C ~/.agentwire checkout damagecontrol.yml",
]

# Marker contexts bash does NOT read as a heredoc: the next line RUNS.
NOT_A_HEREDOC = [
    "echo a # <<'EOF'",
    'echo "a" # <<\'EOF\'',
    "echo a \\<<'EOF'",
    "echo a;# <<'EOF'",
    "true && # <<'EOF'",
    "echo a | # <<'EOF'",
    "(echo a # <<'EOF'\n)",
    "echo $(true # <<'EOF'\n)",
    "echo `true # <<'EOF'\n`",
    'echo "a $(true # <<\'EOF\'\n)"',
    "echo 1<<2",
    "let x=1<<2",
    "[[ a<<b ]]",
    "echo $((1<<2))",
    "((x=1<<2))",
    "let x=1<<EOF",
    "[[ a<<EOF ]]",
    "echo $((1<<EOF))",
    "((x=1<<EOF))",
    "cat <<<EOF",
    "echo '<<EOF'",
    'echo "<<EOF"',
    "echo $'a\\'<<EOF'",
    "cat <<-$\"EOF\"",
    "cat <<",
    # (( )) after any command-position word, not only as a segment's first word
    "for ((i=1<<EOF;i<5;i++)); do break; done",
    "while ((1<<EOF)); do break; done",
    "until ((1<<EOF)); do break; done",
    "if ((1<<EOF)); then :; fi",
    "if true; then ((1<<EOF)); fi",
    "if false; then :; else ((1<<EOF)); fi",
    "time ((1<<EOF))",
    "! ((1<<EOF))",
    "true && ((1<<EOF))",
    "{ ((1<<EOF)); }",
    # legacy $[ ] arithmetic and array subscripts
    "echo $[1<<EOF ]",
    "x=$[1<<EOF ]",
    'echo "$[1<<EOF ]"',
    "echo $(echo $[1<<EOF ])",
    "[[ $[1<<EOF ] ]]",
    "a[1<<EOF ]=x",
    "echo ${a[1<<EOF ]}",
    # a backslash-newline joins lines: the word after it starts fresh
    "echo a \\\n# <<EOF",
    "echo a \\\n\\\n# <<EOF",
    "echo $(echo a \\\n# <<EOF\n)",
    "cat <<EO\\\nF",
    # <( ) / >( ) / name=( ) re-parse their body: the marker closes with the ")"
    "cat <(echo <<EOF)",
    "cat >(cat <<'EOF')",
    "x=(a <<EOF)",
    "a=([1]=<<EOF)",
    "x=(a <<EOF))",
    "x+=(a <<EOF)",
    "declare -a x=(a <<EOF)",
    "FOO=1 x=(a <<EOF)",
    "cat <(cat <(echo <<EOF))",
    "echo $(cat <(echo <<EOF))",
    "true | cat <(echo <<EOF)",
    "echo 2>(cat <<EOF)",
    "cat <(echo ')' <<EOF)",
    # a marker directly in an array literal is never a heredoc, closed or not:
    # bash 3.2 recovers from the syntax error and runs the next line
    "x=(a <<EOF",
    "x=(a <<'EOF'",
    "x=(a <<-EOF",
    "x+=(a <<EOF",
    "n+=(a <<EOF",
    "declare -a n=(a <<EOF",
    "local n=(a <<EOF",
    "export n=(a <<EOF",
    "x=([1]=<<EOF",
    "x=(a\nb <<EOF",
    "x=(a 'q' <<EOF",
    'x=(a "q" <<EOF',
    "x=(a <<EOF\n)",
]

# Contexts where the marker IS a redirection: the body is data, the tail after
# the terminator runs.
REAL_HEREDOC = [
    "cat <<'EOF'",
    "cat <<EOF",
    "cat <<-EOF",
    'cat <<"EOF"',
    "cat <<\\EOF",
    "cat <<'E'OF",
    "cat <<'EO'F",
    'cat <<E"O"F',
    "echo ${#PATH}<<'EOF'",
    "echo a#b; cat <<'EOF'",
    "echo \\\\<<'EOF'",
    "echo $#; cat <<'EOF'",
    "echo ${#PATH}; cat <<'EOF'",
    "echo hi # not a marker\ncat <<'EOF'",
    "cat <<'EOF' # trailing comment",
    "cat 2<<'EOF'",
    "echo $(( 1 + 2 )); cat <<'EOF'",
    "cat \\\n<<'EOF'",
    "echo $[1+2]; cat <<'EOF'",
    "a[1]=x; cat <<'EOF'",
    "if ((1)); then cat <<'EOF'",
    "cat <(echo a) <<'EOF'",
    "cat <<'EOF' <(echo a)",
    "x=(a b); cat <<'EOF'",
    "x=(a $(cat <<'EOF'",
    "( cat <<'EOF'",
    "{ cat <<'EOF'",
    "x=$(cat <<'EOF'",
]


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


def _decision(command, cfg):
    return check_command(command, cfg)["decision"]


def test_bundled_rules_all_loaded(cfg):
    expected = 0
    for f in sorted(RULES_DIR.glob("*.yaml")):
        expected += len((yaml.safe_load(f.read_text()) or {}).get("bashToolPatterns") or [])
    assert expected > 0
    assert len(cfg["bashToolPatterns"]) == expected


def test_non_redirection_markers_open_no_heredoc():
    """The scanner itself: each context yields no heredoc at all."""
    bad = [c for c in NOT_A_HEREDOC if _heredocs(c + "\nbody\nEOF\n")]
    assert not bad, bad


def test_real_markers_still_open_a_heredoc():
    """Control for the test above: a scanner that never fires would pass it."""
    closers = {"( cat <<'EOF'": "\nbody\nEOF\n)", "{ cat <<'EOF'": "\nbody\nEOF\n}", "x=$(cat <<'EOF'": "\nbody\nEOF\n)",
               "x=(a $(cat <<'EOF'": "\nbody\nEOF\n))"}
    bad = [c for c in REAL_HEREDOC if len(_heredocs(c + closers.get(c, "\nbody\nEOF"))) != 1]
    assert not bad, bad


def test_dangerous_line_after_a_non_heredoc_marker_is_blocked(cfg):
    bad = [
        (ctx, tail)
        for ctx in NOT_A_HEREDOC
        for tail in TAILS
        if _decision(f"{ctx}\n{tail}\nEOF", cfg) != "block"
    ]
    assert not bad, bad


def test_dangerous_line_after_a_real_heredoc_is_blocked(cfg):
    """Only the body is hidden: the line after the terminator is scanned."""
    bad = [
        (ctx, tail)
        for ctx in REAL_HEREDOC
        for tail in TAILS
        if _decision(f"{ctx}\nbody\nEOF\n{tail}", cfg) != "block"
        and "$(" not in ctx
    ]
    assert not bad, bad


def test_delimiter_split_by_a_continuation_hides_nothing(cfg):
    """Bash joins ``EO\\<nl>F`` into ``EOF``; a body ending at the first ``EOF`` would
    leave the tail visible, but hiding to a later ``EO`` line must not happen."""
    bad = [t for t in TAILS if _decision(f"cat <<EO\\\nF\nx\nEOF\n{t}\nEO", cfg) != "block"]
    assert not bad, bad


def test_unterminated_heredoc_hides_nothing(cfg):
    """Bash would swallow the rest as the body; the scanner keeps scanning it."""
    bad = [t for t in TAILS if _decision(f"cat <<'EOF'\n{t}", cfg) != "block"]
    assert not bad, bad


PROSE = f"don't do this: {PLAIN}; also rm -rf / (prose)"
CONTROLS = [
    "git commit -F - <<'EOF'\n{B}\nEOF",
    'git commit -F - <<"EOF"\n{B}\nEOF',
    "git commit -F - <<\\EOF\n{B}\nEOF",
    "git commit -F - <<'E'OF\n{B}\nEOF",
    'git commit -F - <<E"O"F\n{B}\nEOF',
    "git commit -F - <<-'EOF'\n\t{B}\n\tEOF",
    "cat <<'EO'F\n{B}\nEOF",
    "cat <<'A' <<'B'\n{B}\nA\n{B}\nB",
    "cat <<'EOF'\n{B}\nEOF\ncat <<'EOF'\n{B}\nEOF",
    "cat <<'EOF'\nit's {B}\nEOF\ncat <<'EOF'\n{B}\nEOF",
    "git commit -m \"$(cat <<'EOF'\n{B}\nEOF\n)\"",
    "( cat <<'EOF'\n{B}\nEOF\n)",
    "{ cat <<'EOF'\n{B}\nEOF\n}",
    "# note about heredocs\ncat <<'EOF'\n{B}\nEOF",
    "echo a#b; cat <<'EOF'\n{B}\nEOF",
    "echo \\\\<<'EOF'\n{B}\nEOF",
    "gh pr create --body-file - <<'EOF'\n{B}\nEOF\ngh pr view",
    "cat <<'EOF' # trailing comment\n{B}\nEOF",
    "cat <<'EOF' > /tmp/x.txt\n{B}\nEOF",
    "cat 2<<'EOF'\n{B}\nEOF",
    "cat <<'EOF' | wc -l\n{B}\nEOF",
    "for ((i=0;i<1;i++)); do cat <<'EOF'\n{B}\nEOF\ndone",
    "echo $[1+2]; cat <<'EOF'\n{B}\nEOF",
    "cat \\\n<<'EOF'\n{B}\nEOF",
    "cat <(echo a) <<'EOF'\n{B}\nEOF",
    "x=(a b); cat <<'EOF'\n{B}\nEOF",
]


UNQUOTED = [
    "git commit -F - <<EOF\n{B}\nEOF",
    "git commit -F - <<-EOF\n\t{B}\n\tEOF",
    "cat <<EOF\n{B}\nEOF\ncat <<'EOF'\n{B}\nEOF",
]


def test_prose_in_a_real_heredoc_body_stays_hidden(cfg):
    """A control's decision must equal the same command with an innocuous body."""
    bad = [
        c
        for c in CONTROLS
        if _decision(c.replace("{B}", PROSE), cfg) != _decision(c.replace("{B}", "hello"), cfg)
    ]
    assert not bad, bad
    # an unquoted body is text too, but an apostrophe in it reads as a quote
    prose = PROSE.replace("'", "")
    bad = [
        c
        for c in UNQUOTED
        if _decision(c.replace("{B}", prose), cfg) != _decision(c.replace("{B}", "hello"), cfg)
    ]
    assert not bad, bad
    assert _decision(CONTROLS[0].replace("{B}", PROSE), cfg) == "allow"


def test_unbalanced_parens_in_a_substitution_heredoc_are_not_hidden():
    """Older bash matches ``$(…)`` by counting parentheses, blind to heredocs, so a
    body with a stray ``)`` can end the span early; such a body is scanned."""
    balanced = "x=$(cat <<'EOF'\nfix (the thing)\nEOF\n)"
    stray = "x=$(cat <<'EOF'\nfix the thing)\nEOF\n)"
    opened = "x=$(cat <<'EOF'\nfix (the thing\nEOF\n)"
    assert [len(_heredocs(c)) for c in (balanced, stray, opened)] == [1, 0, 0]


def test_unclosed_array_literal_holds_no_heredoc():
    """An unclosed ``name=(`` is a syntax error bash 3.2 recovers from, so even a
    heredoc nested in a ``$(…)`` inside it is not trusted; a closed one is read."""
    unclosed = "x=(a $(cat <<'EOF'\nbody\nEOF\n)\nrm -rf /\n"
    closed = "x=(a $(cat <<'EOF'\nbody\nEOF\n))\nls\n"
    assert [len(_heredocs(c)) for c in (unclosed, closed)] == [0, 1]
