"""A quoted heredoc stored by ``cat``/``tee`` is text, not a read of the secrets it names (#940).

Writing ``.env`` into a ``.gitignore``, or ``event.key`` into a script, mentions
a secret's name without touching it; the zero-access rule blocked both. The
other half is the one that matters: the same text fed to anything that could
USE it (an interpreter, xargs, a shell pipe), an unquoted body, the destination
path, and any command after the body keep blocking.
"""

from pathlib import Path

import pytest

from agentwire.safety._core import check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"

STORED_TEXT = {
    "gitignore": "cat > .gitignore <<'EOF'\n.DS_Store\n.env\nEOF",
    "append-script": "cat >> src/a.ts <<'EOF'\nif (event.key !== x) return;\nEOF",
    "tee": "tee notes.md <<'EOF'\nsecrets live in ~/.pypirc\nEOF",
    "after-cd": "cd /tmp/x && cat > brief.md <<'EOF'\nkeys come from the .env wiring\nEOF",
}

STILL_BLOCKS = {
    "destination-is-the-secret": "cat > .env <<'EOF'\nA=1\nEOF",
    "destination-after-and": "cd x && cat > ~/.pypirc <<'EOF'\nA=1\nEOF",
    "interpreter-body": "python3 <<'EOF'\nopen('server.key')\nEOF",
    "piped-to-shell": "cat <<'EOF' | sh\ncat .env\nEOF",
    "fed-to-xargs": "xargs cat <<'EOF'\n.env\nEOF",
    "unquoted-body": "cat > f <<EOF\n$(cat .env)\nEOF",
    "command-after-body": "cat > f <<'EOF'\nhi\nEOF\ncat .env",
    "plain-read": "cat .env",
}


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    assert c.get("zeroAccessPaths"), "bundled rules failed to load"
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


def test_text_stored_by_cat_or_tee_is_allowed(cfg):
    wrongly_stopped = {n: check_command(c, cfg, cwd="/tmp")["decision"] for n, c in STORED_TEXT.items()
                       if check_command(c, cfg, cwd="/tmp")["decision"] != "allow"}
    assert not wrongly_stopped, wrongly_stopped


def test_anything_that_could_use_the_text_still_blocks(cfg):
    wrongly_allowed = [n for n, c in STILL_BLOCKS.items() if check_command(c, cfg, cwd="/tmp")["decision"] == "allow"]
    assert not wrongly_allowed, wrongly_allowed
