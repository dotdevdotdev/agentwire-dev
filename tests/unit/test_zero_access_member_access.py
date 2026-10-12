"""An extension glob (``*.key``) must not fire on JS property access or a jq path.

Two halves, both load-bearing: expression-shaped tokens are released, and every
shape a real secret read takes keeps matching — including the ones a careless
release would hide (a second token behind a released one, brace expansion, a
quoted filename, a path).
"""

from pathlib import Path

import pytest

from agentwire.safety._core import check_command, load_config, matches_path_in_command

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"

# Code, not a path argument: nothing here names a file.
RELEASED = {
    "js-if": "if (event.key !== themeStorageKey) return;",
    "js-ternary": "lines.push((e.key !== undefined ? 1 : 0));",
    "js-template": "console.log(`${license.key} pressed`)",
    "jq-field": "gh search repos x -q '.[]|\"\\(.fullName) \\(.license.key)\"'",
    "js-index": "handlers[e.key](ev)",
}

# A secret-file read in every spelling that must keep blocking.
STILL_MATCHES = {
    "cat": "cat server.key",
    "cat-path": "cat ./certs/server.key",
    "quoted": "python3 -c \"open('server.key').read()\"",
    "openssl": "openssl rsa -in a.key -out b.pem",
    "scp": "scp host:k.key .",
    "released-then-real": "(e.key a.key",
    "real-after-released": "if (event.key !== x) cat server.key",
    "redirect": "tee < private.key",
}

# Known limit, not asserted either way: a bare ``x.key`` after a space (``not e.key``)
# has the exact shape of ``cat a.key`` and keeps matching.


def test_code_shaped_tokens_are_released():
    wrongly_matched = [n for n, c in RELEASED.items() if matches_path_in_command("*.key", c)]
    assert not wrongly_matched, wrongly_matched


def test_real_secret_reads_still_match():
    missed = [n for n, c in STILL_MATCHES.items() if not matches_path_in_command("*.key", c)]
    assert not missed, missed


def test_whole_command_is_decided_by_the_real_rules():
    cfg = load_config(RULES_DIR)
    cfg["safety"] = {"enabled": True, "disabled_rules": []}
    code = "node -e \"process.stdin.on('keypress', (s, e) => { if (e.key === 'q') process.exit() })\""
    assert check_command(code, cfg)["decision"] == "allow"
    assert check_command("cat server.key", cfg)["decision"] == "block"
