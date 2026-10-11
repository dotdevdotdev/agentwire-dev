"""Heredoc bodies and literal backticks are DATA, not shell syntax.

Every ordinary multi-line commit and ``python3 - <<'EOF'`` used to resolve to a
confirm dialog even under ``auto``: the ambiguity fallback tokenized the
heredoc body as shell (an apostrophe read as "unbalanced quotes", parentheses
as a "subshell") and read a quoted or escaped backtick as an unbalanced
command substitution. ``inert_view`` neutralizes exactly those spans for the
ambiguity analysis only.

Two halves, both load-bearing (a guard has a false-accept and a false-reject):
  * DATA shapes must no longer be unverifiable;
  * the shapes that really conceal a verb must STILL be, including the ones
    a careless neutralizer would hide — an unquoted heredoc, a marker inside a
    string, a heredoc feeding a shell, an unterminated heredoc.
Rule matching still reads the raw command, so a protected path inside a body
keeps blocking.
"""

from pathlib import Path

import pytest

from agentwire.safety._core import (
    ambiguity_conceals_verb,
    check_command,
    inert_view,
    load_config,
    normalize_subcommands,
)

pytestmark = pytest.mark.real_agentwire_home

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
_RF = "-r" + "f"  # keep the literal flag pair out of this file's own text


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    assert c.get("bashToolPatterns"), "bundled rules failed to load"
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


COMMIT_IDIOM = (
    'git commit -m "$(cat <<\'EOF\'\n'
    "fix: use `grep` — don't break (ever)\n"
    "EOF\n"
    ')"'
)

DATA_SHAPES = {
    "python-stdin-heredoc": "python3 - <<'EOF'\nprint(\"it's\")\nx = (1, 2)\nEOF",
    "cat-redirect-heredoc": "cat > notes.txt <<'EOF'\ndon't (stop) `now`\nEOF",
    "double-quoted-delim": 'cat > a.txt <<"EOF"\nit\'s (fine)\nEOF',
    "backslash-delim": "cat > a.txt <<\\EOF\nit's (fine)\nEOF",
    "dash-heredoc": "cat > a.txt <<-'EOF'\n\tit's (fine)\n\tEOF",
    "escaped-backtick-in-dquotes": 'grep -rn -E "altKey|\\`" src',
    "backtick-in-squotes": "echo '`not a substitution`'",
    "dollar-paren-in-squotes": "echo '$(not run)'",
}


@pytest.mark.parametrize("name", sorted(DATA_SHAPES))
def test_data_shapes_are_not_ambiguous(name, cfg):
    command = DATA_SHAPES[name]
    _subs, ambiguous = normalize_subcommands(command)
    assert ambiguous is None, ambiguous
    assert ambiguity_conceals_verb(command) is None
    assert check_command(command, cfg)["decision"] == "allow"


def test_commit_idiom_is_not_unverifiable(cfg):
    """The ``-m "$(cat <<'EOF' …)"`` idiom keeps its (bypass-demotable) operand
    ask — it must just never be classed as verb concealment."""
    assert ambiguity_conceals_verb(COMMIT_IDIOM) is None
    assert not check_command(COMMIT_IDIOM, cfg).get("unverifiable")


# ---------------------------------------------------------------------------
# Must-fail controls — the neutralizer must not become a place to hide a verb.
# ---------------------------------------------------------------------------

STILL_AMBIGUOUS = {
    "unquoted-heredoc-expands": "cat > f <<EOF\n$(date)\nEOF",
    "unterminated-heredoc": "cat <<'EOF'\nit's never closed",
    "real-backtick-in-dquotes": 'echo "`id`"',
    "unquoted-backtick": "echo `id`",
    "bare-substitution": "echo $(id)",
}


@pytest.mark.parametrize("name", sorted(STILL_AMBIGUOUS))
def test_live_syntax_is_still_ambiguous(name):
    _subs, ambiguous = normalize_subcommands(STILL_AMBIGUOUS[name])
    assert ambiguous is not None


STILL_CONCEALING = {
    # a marker inside a string opens no heredoc — hiding what follows would hide a real eval
    "marker-inside-string": "echo \"<<'EOF'\"\neval $(x)\nEOF",
    # the body of a heredoc feeding a shell IS the program
    "heredoc-feeds-shell": "bash <<'EOF'\n$(echo rm) " + _RF + " /x\nEOF",
    "heredoc-feeds-ssh": "ssh host <<'EOF'\n$(echo rm) " + _RF + " /x\nEOF",
    "substitution-decides-verb": "$(echo rm) " + _RF + " /x",
    "eval": "eval \"$cmd\"",
}


@pytest.mark.parametrize("name", sorted(STILL_CONCEALING))
def test_verb_concealment_is_still_unverifiable(name, cfg):
    command = STILL_CONCEALING[name]
    assert ambiguity_conceals_verb(command) is not None
    result = check_command(command, cfg)
    assert result["decision"] in ("ask", "block")
    if result["decision"] == "ask":
        assert result.get("unverifiable")


def test_rules_still_read_heredoc_bodies(cfg):
    """The inert view is for the ambiguity fallback only: a protected path in a
    body or redirect keeps blocking because rules read the raw command."""
    body = "cat <<'EOF' | tee notes.txt\n~/.ssh/id_rsa\nEOF"
    redirect = "cat > ~/.ssh/id_rsa <<'EOF'\nx\nEOF"
    assert check_command(body, cfg)["decision"] == "block"
    assert check_command(redirect, cfg)["decision"] == "block"


def test_inert_view_only_touches_quoted_delimiter_bodies():
    unquoted = "cat > f <<EOF\n$(date)\nEOF"
    assert inert_view(unquoted) == unquoted
    quoted = "cat > f <<'EOF'\nsecret body\nEOF"
    assert "secret body" not in inert_view(quoted)
    assert inert_view(quoted).startswith("cat > f <<'EOF'\n")
    assert inert_view(quoted).endswith("\nEOF")
