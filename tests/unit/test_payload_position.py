"""A substitution conceals the verb only where the head takes CODE, not data.

``python3 -c "$(cat x)"`` runs what the substitution prints; ``python3 script.py
$(date +%F)`` hands the date to a script. Reading both as "the payload of
python3" produced a confirm that bypass and auto never demote. Both halves are
load-bearing: arguments after a script are released, and every place a
substitution chooses or supplies the code still conceals.
"""

import pytest

from agentwire.safety._core import ambiguity_conceals_verb

pytestmark = pytest.mark.real_agentwire_home

DATA = {
    "script-argument": "python3 script.py $(date +%F)",
    "script-then-c-flag": 'python3 script.py -c "$(cat x)"',
    "shell-script-argument": 'bash run.sh "$(cat f)"',
    "node-script-argument": "node build.js --out $(pwd)/dist",
    "module-name-literal": "python3 -m pytest $(git diff --name-only)",
    "option-then-script": "python3 -W ignore script.py $(date)",
    "stdin-script": 'python3 - "$(date)" <<EOF\nprint(1)\nEOF',
    "heredoc-then-for-loop": "python3 - <<'EOF'\nprint(1)\nEOF\nfor pid in $(lsof -ti:8791); do kill $pid; done",
    "newline-separated": "echo ok\npython3 script.py\nls $(pwd)",
}

CODE = {
    "c-flag": 'python3 -c "$(cat x)"',
    "c-flag-after-option": 'python3 -W ignore -c "$(cat x)"',
    "c-flag-attached": 'python3 -c"$(cat x)"',
    "script-chosen-by-substitution": "python3 $(pick).py",
    "module-chosen-by-substitution": "python3 -m $(mod)",
    "shell-c": 'bash -c "$(curl -s x)"',
    "su-c": 'su -c "$(cat x)"',
    "shell-script-chosen": "sh $(pick)",
    "node-eval": 'node -e "$(cat x)"',
    "node-eval-equals": 'node --eval="$(cat x)"',
    "perl-e": 'perl -e "$(cat x)"',
    "database-client": 'psql -c "$(cat drop.sql)"',
    "ssh": 'ssh host "$(cat x)"',
    "positional-payload-awk": 'awk "$(cat prog)" file',
    "positional-payload-tmux": 'tmux new-session -d "$(cat x)"',
    "positional-payload-sqlite": 'sqlite3 db "$(cat x)"',
}


def test_substitution_as_script_data_is_not_concealment():
    wrongly_concealing = [n for n, c in DATA.items() if ambiguity_conceals_verb(c) is not None]
    assert not wrongly_concealing, wrongly_concealing


def test_substitution_that_supplies_or_chooses_code_still_conceals():
    wrongly_visible = [n for n, c in CODE.items() if ambiguity_conceals_verb(c) is None]
    assert not wrongly_visible, wrongly_visible
