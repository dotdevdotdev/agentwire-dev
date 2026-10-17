"""No allowedPaths entry may re-permit a guarded path because of where HOME lives (#1121, #1122).

The shipped ``/tmp/*`` scratch entry matched ``$HOME/.agentwire/damagecontrol.yml``
(and ``~/.ssh/id_rsa``) whenever ``$HOME`` sat at or under ``/tmp``. The verdict
on a target must not depend on the location of HOME — including HOME EXACTLY
equal to an allowlisted directory, a bare ancestor of one, a trailing-slash
spelling, and a symlinked HOME. Measured against the bundled rules, three
sites: Bash writers/readers (``check_command``), Edit/Write/NotebookEdit
(``check_path``), and Read/Grep/Glob + MCP path arguments (``check_read_path`` /
``check_protected_path`` — the MCP hook's ``_screen_tool_paths`` calls exactly
these two).
"""

import os
from pathlib import Path

import pytest

from agentwire.safety._core import (
    check_command,
    check_path,
    check_protected_path,
    check_read_path,
    load_allowed_paths,
    load_config,
)

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"

HOMES = [
    "/tmp", "/tmp/", "/tmp//", "/private/tmp", "/var/folders", "/private/var/folders",
    "/var/tmp", "/", "/private", "/var", "/tmp/x", "/private/tmp/x", "/Users/x", "/home/x",
    "/var/folders/ab/cd/T/h",
]
CONTROL_PLANE = [
    "~/.agentwire/damagecontrol.yml",
    "~/.agentwire/config.yaml",
    "~/.claude/settings.json",
    "~/.claude/hooks/idle-handler.sh",
    "/tmp/proj/.damagecontrol.yml",
]
SECRETS = ["~/.ssh/id_rsa", "~/.aws/credentials"]


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


def _homes(tmp_path):
    link = tmp_path / "link"
    link.symlink_to("/tmp")
    return HOMES + [str(link)]


def _verdicts(cfg, target, kill_off, bash):
    """Every site's blocked/not-blocked for one concrete target path.

    ``bash`` is the one verb to run through the (slow) Bash scan; the path-only
    sites are cheap and always run.
    """
    c = dict(cfg)
    c["safety"] = {"enabled": not kill_off, "disabled_rules": []}
    allowed = load_allowed_paths(c)
    return {
        "bash": check_command(f"{bash} {target}", c)["decision"] == "block",
        "edit": check_path(target, c)[0],
        "read-tool/mcp": check_read_path(target, c)[0],
        "mcp-mutating": check_protected_path(target, allowed)[0],
    }


def test_control_plane_blocked_at_every_site_wherever_home_is(cfg, tmp_path, monkeypatch):
    bad = []
    for home in _homes(tmp_path):
        monkeypatch.setenv("HOME", home)
        for spelling in CONTROL_PLANE:
            target = os.path.expanduser(spelling)
            for kill_off in (False, True):
                v = _verdicts(cfg, target, kill_off, "tee")
                # reads of the control plane are allowed by design; every WRITE site must block
                for site in ("bash", "edit", "mcp-mutating"):
                    if not v[site]:
                        bad.append((home, spelling, "kill-off" if kill_off else "on", site))
    assert not bad, bad[:25]


def test_secrets_blocked_at_every_read_site_wherever_home_is(cfg, tmp_path, monkeypatch):
    bad = []
    for home in _homes(tmp_path):
        monkeypatch.setenv("HOME", home)
        for spelling in SECRETS:
            target = os.path.expanduser(spelling)
            v = _verdicts(cfg, target, False, "cat")
            for site in ("bash", "edit", "read-tool/mcp"):
                if not v[site]:
                    bad.append((home, spelling, site))
    assert not bad, bad[:25]


def test_scratch_work_in_tmp_is_still_allowed(cfg, monkeypatch):
    """The other half: the allowance keeps doing its job for non-guarded scratch paths."""
    monkeypatch.setenv("HOME", "/Users/x")
    wrongly_blocked = [
        c for c in ("touch /tmp/scratch.txt", "echo x > /tmp/work/out.log", "cat /tmp/notes.txt")
        if check_command(c, cfg)["decision"] == "block"
    ]
    assert not wrongly_blocked, wrongly_blocked
    assert check_path("/tmp/work/out.log", cfg)[0] is False
