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


# ---- test-gap pins (round-2 review): no-delete / read-only / anchoring helpers ----

NO_DELETE_TARGETS = [
    "~/.claude/CLAUDE.md",
    "~/.agentwire/sessions/s1/metadata.json",
    "~/.claude/projects/p1",
]
# ``mv`` away is not here: it is not a delete verb to the no-delete ladder at ANY
# home (a HOME-independent policy question, tracked separately). ``find -delete``
# IS: the bypassable-rule override once waived it via ``/tmp/*`` (#1126).
DELETE_SHAPES = [
    "rm @T", "rmdir @T", "unlink @T", "rm -r @T",
    "find @T -delete",
    "find @T -type f -delete",
    "find @T -exec rm {} +",
]
READ_ONLY_TARGETS = ["~/.bashrc", "~/.zshrc", "~/.bash_history"]
WRITE_SHAPES = ["echo x >> @T", "tee @T < /dev/null", "sed -i s/a/b/ @T"]


def test_no_delete_set_is_blocked_for_every_delete_verb_wherever_home_is(cfg, tmp_path, monkeypatch):
    bad = []
    for home in [*_homes(tmp_path), "/tmp/x", "/Users/x", f"/tmp/claude-{os.getuid()}/h"]:  # last: inside the rm/find scratch carve-out
        monkeypatch.setenv("HOME", home)
        for spelling in NO_DELETE_TARGETS:
            target = os.path.expanduser(spelling)
            for shape in DELETE_SHAPES:
                command = shape.replace("@T", target)
                if check_command(command, cfg)["decision"] != "block":
                    bad.append((home, command))
    assert not bad, bad[:25]


def test_read_only_set_is_blocked_for_bash_writers_wherever_home_is(cfg, tmp_path, monkeypatch):
    bad = []
    for home in _homes(tmp_path):
        monkeypatch.setenv("HOME", home)
        for spelling in READ_ONLY_TARGETS:
            target = os.path.expanduser(spelling)
            for shape in WRITE_SHAPES:
                command = shape.replace("@T", target)
                if check_command(command, cfg)["decision"] != "block":
                    bad.append((home, command))
            if not check_path(target, cfg)[0]:  # the Edit/Write half of the read-only guard
                bad.append((home, "edit " + target))
    assert not bad, bad[:25]


def test_literal_entry_naming_a_guarded_file_still_repermits(cfg, monkeypatch):
    """False-block side: a human's entry that NAMES the file (or scopes a guarded directory) works."""
    bad = []
    for home in ("/Users/x", "/tmp/x"):
        monkeypatch.setenv("HOME", home)
        secret = os.path.expanduser(SECRETS[0])
        lock = f"{home}/proj/poetry.lock"
        c = dict(cfg)
        c["allowedPaths"] = list(cfg.get("allowedPaths", [])) + [
            {"path": secret, "allow": {"read"}},
            {"path": lock, "allow": {"edit", "write"}},
        ]
        if check_read_path(secret, c)[0]:
            bad.append((home, "literal secret entry ignored"))
        if check_path(lock, c)[0]:
            bad.append((home, "literal read-only entry ignored"))
        if not check_path(f"{home}/proj/yarn.lock", c)[0]:
            bad.append((home, "literal entry widened to a sibling"))
        # directory-pattern clause: the shipped */dist/* entry re-permits inside a guarded dist/
        if check_path(f"{home}/proj/dist/app.js", c)[0]:
            bad.append((home, "dir-pattern entry ignored"))
        if check_command(f"rm {home}/proj/dist/old.whl", c)["decision"] == "block":
            bad.append((home, "dir-pattern delete entry ignored"))
    assert not bad, bad


def test_anchor_home_rewrites_by_component_not_by_prefix(monkeypatch):
    from agentwire.safety import _core

    monkeypatch.setenv("HOME", "/tmp/x")
    # a sibling whose name merely STARTS with the home string is not below it
    assert _core._anchor_home("/tmp/xyz/.agentwire/a") == "/tmp/xyz/.agentwire/a"
    assert _core._anchor_home("/tmp/x/.agentwire/a") == _core._CANONICAL_HOME + "/.agentwire/a"
    assert _core._anchor_home("/tmp/*") == "/tmp/*"


def test_anchor_home_resolves_a_symlinked_home(tmp_path, monkeypatch):
    """An entry spelled with the RESOLVED home still anchors when $HOME is the symlink."""
    from agentwire.safety import _core

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(link))
    _core._live_homes.cache_clear()
    assert _core._anchor_home(f"{real.resolve()}/.agentwire/*") == _core._CANONICAL_HOME + "/.agentwire/*"
    assert _core._anchor_home(f"{link}/.agentwire/*") == _core._CANONICAL_HOME + "/.agentwire/*"


def test_unguarded_scratch_delete_still_waived_by_the_location_entry(cfg, monkeypatch):
    """Other half of #1126: the per-path split keeps /tmp scratch work working."""
    monkeypatch.setenv("HOME", "/Users/x")
    wrongly_blocked = [
        c for c in ("find /tmp/work -name '*.o' -delete", "rm /tmp/work/out.log", "rm /var/tmp/build/a.o")
        if check_command(c, cfg)["decision"] == "block"
    ]
    assert not wrongly_blocked, wrongly_blocked
