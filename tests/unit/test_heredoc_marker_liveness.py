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

from damage_control.engine import check_command, load_config
from damage_control.engine import bundled_rule_sources

pytestmark = pytest.mark.real_agentwire_home

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = bundled_rule_sources()

PLAIN = "cd ~/.agentwire && touch damagecontrol.yml"
TAILS = [
    "rm -rf /",
    PLAIN,
    "echo x >| ~/.agentwire/damagecontrol.yml",
    "git -C ~/.agentwire checkout damagecontrol.yml",
]

# Marker contexts bash does NOT read as a heredoc: the next line RUNS.

# Contexts where the marker IS a redirection: the body is data, the tail after
# the terminator runs.
REAL_HEREDOC = [
    "cat <<'EOF'",
    "cat <<EOF",
    "cat <<-EOF",
    'cat <<"EOF"',
    'cat <<$"EOF"',  # locale-quoted: quotes like "EOF" (#1132)
    'cat <<-$"EOF"',
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
    "a=($((1<<2))) <<'EOF'",
    "a=(\"<<\") <<'EOF'",
    "a=('<<') <<'EOF'",
    "a=(x) <<'EOF'",
    "a=(x\ny) <<'EOF'",
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


def test_dangerous_line_after_a_real_heredoc_is_blocked(cfg):
    """Only the body is hidden: the line after the terminator is scanned."""
    bad = [
        (ctx, tail)
        for ctx in REAL_HEREDOC
        for tail in TAILS
        if _decision(f"{ctx}\nbody\nEOF\n{tail}", cfg) != "block" and "$(" not in ctx
    ]
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
    "a=($((1<<2))); cat <<'EOF'\n{B}\nEOF",
    "a=(x) <<'EOF'\n{B}\nEOF",
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
