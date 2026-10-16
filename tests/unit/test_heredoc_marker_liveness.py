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
    closers = {"( cat <<'EOF'": "\nbody\nEOF\n)", "{ cat <<'EOF'": "\nbody\nEOF\n}", "x=$(cat <<'EOF'": "\nbody\nEOF\n)"}
    bad = [c for c in REAL_HEREDOC if len(_heredocs(c + closers.get(c, "\nbody\nEOF"))) != 1]
    assert not bad, bad


def test_dangerous_line_after_a_non_heredoc_marker_is_blocked(cfg):
    bad = [
        (ctx, tail)
        for ctx in NOT_A_HEREDOC
        for tail in TAILS
        if _decision(f"{ctx}\n{tail}", cfg) != "block"
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
