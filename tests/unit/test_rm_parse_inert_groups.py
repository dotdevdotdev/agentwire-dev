"""A harmless subshell group no longer makes the whole compound unmodelled (#940).

``(npm run start >/dev/null 2>&1 &); sleep 4; rm shot.tmp.mjs`` is the usual way to
screenshot a dev server: the group starts a server, the ``rm`` cleans up a
file in the session scratch dir. The scratch-rm allowance gave up on ANY group
and the agent was blocked on its own temp file. The other half: a group that
could change the directory or variables the ``rm`` is judged against (cd,
assignment, export) or that holds a verb under judgment must still make the
command unmodelled, because modelling it would leak a subshell's state.
"""

import os
import uuid
from pathlib import Path

import pytest

from agentwire.safety._core import _parse_rm_command, check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"

MODELLED = {
    "server-then-rm": "(npm run start >/dev/null 2>&1 &); sleep 4; rm shot.tmp.mjs",
    "redirect-var": "(npx next start --port 3198 > $S/main.log 2>&1 &); rm -f x.log",
}

STAYS_UNMODELLED = {
    "cd-in-group": "(cd /etc && true); rm f",
    "assignment-in-group": "(S=/etc); rm $S/f",
    "export-in-group": "(export S=/etc); rm $S/f",
    "rm-in-group": "(rm -rf x); rm f",
    "git-in-group": "(git checkout other); rm f",
}


@pytest.fixture
def scratch():
    d = Path(f"/private/tmp/claude-{os.getuid()}") / f"proj-{uuid.uuid4().hex[:8]}" / "sess" / "scratchpad"
    d.mkdir(parents=True)
    yield d
    for f in d.iterdir():
        f.unlink()
    os.removedirs(d)


def test_harmless_groups_do_not_poison_the_model(scratch):
    unmodelled = [n for n, c in MODELLED.items() if _parse_rm_command(c, str(scratch)) is None]
    assert not unmodelled, unmodelled


def test_groups_that_could_leak_state_stay_unmodelled(scratch):
    modelled = [n for n, c in STAYS_UNMODELLED.items() if _parse_rm_command(c, str(scratch)) is not None]
    assert not modelled, modelled


def test_scratch_cleanup_after_a_server_group_is_allowed_but_a_real_file_is_not(scratch):
    cfg = load_config(RULES_DIR)
    cfg["safety"] = {"enabled": True, "disabled_rules": []}
    (scratch / "shot.tmp.mjs").write_text("x")
    cmd = "(npm run start >/dev/null 2>&1 &); sleep 4; rm shot.tmp.mjs"
    assert check_command(cmd, cfg, cwd=str(scratch))["decision"] == "allow"
    must_block = "(npm run start >/dev/null 2>&1 &); sleep 4; rm -rf /etc/hosts"
    assert check_command(must_block, cfg, cwd=str(scratch))["decision"] == "block"
