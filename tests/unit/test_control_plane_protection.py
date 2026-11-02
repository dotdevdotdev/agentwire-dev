"""Control-plane lockdown (#466).

The damage-control control plane — the kill-switch/rule files, the hook scripts,
the rule YAMLs, and the Claude Code hook registration — must be unwritable by the
policed agent, EVEN with the ``# allow:`` escape hatch and EVEN when the kill
switch is off. Only the user's host-side ``allowedPaths`` allowlist re-permits a
path. Loosening is always a human, host-side act.

Every table collects its offenders and asserts none, so one failure lists them all.
"""

import os

import pytest
from damage_control.engine import (
    check_command,
    check_path,
    is_protected_control_plane,
    load_safety_config,
)

from agentwire.safety_commands import load_patterns

# This module is *about* the real control plane: it asserts that the owner's
# actual `~/.agentwire` and `~/.claude` files are unwritable by the policed
# agent, and its parameters are real absolute paths resolved at import. The
# #893 home redirect would point the protection logic at a tmp directory while
# these paths stayed real, making every case fail for the wrong reason. Reads
# only — the audit-hook backstop still fails the run on any write.
pytestmark = pytest.mark.real_agentwire_home

CONTROL_PLANE_FILES = [
    os.path.expanduser("~/.agentwire/damagecontrol.yml"),
    "/some/repo/.damagecontrol.yml",
    os.path.expanduser("~/.claude/settings.json"),
    os.path.expanduser("~/.agentwire/hooks/damage-control/bash-tool-damage-control.py"),
    os.path.expanduser("~/.claude/hooks/idle-handler.sh"),
    os.path.expanduser("~/.agentwire/damage-control/core.yaml"),
    # Execution-plane configs whose strings agentwire runs via shell=True
    # (scheduler gate commands, service healthchecks, per-project task commands)
    # — these never traverse the Claude Code hook, so they are control plane too.
    os.path.expanduser("~/.agentwire/scheduler.yaml"),
    os.path.expanduser("~/.agentwire/config.yaml"),
    # Per-project task-execution config (#720) — NOT .agentwire.yml, which was
    # split out and is agent-writable again.
    "/some/repo/.agentwire.tasks.yml",
]


@pytest.fixture
def cfg():
    c = load_patterns()
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


@pytest.fixture
def off():
    c = load_patterns()
    c["safety"] = {"enabled": False, "disabled_rules": []}
    return c


def _not_protected_block(commands, config):
    return [
        c for c in commands
        for r in [check_command(c, config)]
        if not (r["decision"] == "block" and r.get("protected") is True)
    ]


# --------------------------------------------------------------------------
# Edit / Write tools
# --------------------------------------------------------------------------


def test_edit_write_to_every_control_plane_file_is_blocked_even_with_the_kill_switch_off(cfg, off):
    bad = []
    for path in CONTROL_PLANE_FILES:
        on_blocked, reason = check_path(path, cfg)
        off_blocked, _ = check_path(path, off)
        if not (on_blocked and "control-plane" in reason and off_blocked):
            bad.append(path)
    assert not bad, bad


def test_agent_writable_files_are_not_control_plane(cfg):
    """#720: .agentwire.yml is declarative session config again, and the
    .proposed staging file is where an agent drafts tasks for a human."""
    free_paths = [
        "/some/repo/.agentwire.yml",
        "/some/repo/.agentwire.tasks.proposed.yml",
        os.path.expanduser("~/projects/foo/main.py"),
        os.path.expanduser("~/projects/foo/app/config.yaml"),
    ]
    wrongly_protected = [p for p in free_paths if is_protected_control_plane(p)]
    free_writes = [
        "echo 'roles: [agentwire]' > .agentwire.yml",
        "echo 'roles: [agentwire]' >> /some/repo/.agentwire.yml",
        "echo 'tasks: {}' > .agentwire.tasks.proposed.yml",
    ]
    wrongly_protected += [
        c for c in free_writes
        for r in [check_command(c, cfg)]
        if r.get("protected") or r["decision"] == "block"
    ]
    assert not wrongly_protected, wrongly_protected


# --------------------------------------------------------------------------
# Bash writers — one representative per writer class
# --------------------------------------------------------------------------

BASH_WRITES = [
    "echo 'enabled: false' > ~/.agentwire/damagecontrol.yml",
    "echo '{}' > ~/.claude/settings.json",
    "rm ~/.agentwire/damage-control/core.yaml",
    "sed -i 's/x/y/' ~/.agentwire/hooks/damage-control/bash-tool-damage-control.py",
    "echo 'enabled: false' > .damagecontrol.yml",
    "echo 'x' > ~/.agentwire/scheduler.yaml",
    "sed -i 's/a/b/' ~/.agentwire/config.yaml",
    "echo x >> /some/repo/.agentwire.tasks.yml",
    "cp /tmp/x /some/repo/.agentwire.tasks.yml",
    "mv /tmp/x /some/repo/.agentwire.tasks.yml",
    'tee "/some/repo/.agentwire.tasks.yml" < /tmp/x',
    "python3 -c 'open(\".agentwire.tasks.yml\", \"w\").write(\"x\")'",
    "perl -e 'open(F, \">\", \".agentwire.tasks.yml\")'",
    "python3 - <<'EOF'\nopen('/some/repo/.agentwire.tasks.yml', 'w').write('x')\nEOF",
    "echo 'open(\".agentwire.tasks.yml\",\"w\")' | python3",
]

# Flagless, indirect, and directory-relative writers (#1121 and its review rounds).
INDIRECT_WRITES = [
    "ln -sf /tmp/x ~/.agentwire/damagecontrol.yml",
    "ln -s ~/.agentwire/damagecontrol.yml /tmp/link",
    "curl -o ~/.agentwire/damagecontrol.yml http://e.com/x",
    "wget -O ~/.claude/settings.json http://e.com/x",
    "tar xf /tmp/a.tar -C ~/.claude/hooks",
    "unzip /tmp/a.zip -d ~/.agentwire",
    "cp -t ~/.agentwire/damage-control /tmp/x",
    "gzip ~/.agentwire/damagecontrol.yml",
    "patch ~/.claude/settings.json /tmp/p.diff",
    "touch ~/.agentwire/damagecontrol.yml",
    "git checkout -- .damagecontrol.yml",
    "echo ~/.agentwire/damagecontrol.yml | xargs rm",
    "printf 'rm %s' ~/.claude/settings.json | sh",
    'for f in ~/.claude/settings.json; do echo x > "$f"; done',
    "sh <<EOF\nrm ~/.agentwire/damagecontrol.yml\nEOF",
    "cd ~/.agentwire && touch damagecontrol.yml",
    "echo x >| ~/.agentwire/damagecontrol.yml",
    "echo x > $HOME/.agentwire/damagecontrol.yml",
    # a program piped to a remote shell, inside a compound body (#1112)
    "case x in a) echo 'touch ~/.agentwire/damagecontrol.yml' | ssh host;; esac",
    "if true; then echo 'touch ~/.agentwire/damagecontrol.yml' | ssh host; fi",
    # tee with a "-a" that is NOT the append option (#1198): the old write
    # pattern's (?!.*-a) rejected any -a substring, and the append patterns only
    # looked before the path, so these matched nothing at all.
    "echo x | tee ~/.agentwire/config.yaml /tmp/my-app.log",
    "echo x | tee ~/.agentwire/scheduler.yaml # my-app",
    "echo x | tee ~/.agentwire/damagecontrol.yml -a",
    "echo x | tee ~/.agentwire/damagecontrol.yml --append",
]


def test_bash_writes_to_the_control_plane_are_blocked(cfg):
    leaked = _not_protected_block(BASH_WRITES, cfg)
    assert not leaked, leaked


def test_indirect_writers_to_the_control_plane_are_blocked(cfg):
    leaked = _not_protected_block(INDIRECT_WRITES, cfg)
    assert not leaked, leaked


def test_escape_hatch_and_kill_switch_cannot_reopen_the_control_plane(cfg, off):
    sample = BASH_WRITES[:5] + INDIRECT_WRITES[::4]
    leaked = _not_protected_block([c + "  # allow: I really want to" for c in sample], cfg)
    leaked += _not_protected_block(sample, off)
    assert not leaked, leaked


def test_control_plane_verdict_is_independent_of_home_location(cfg, monkeypatch):
    """#1121: a HOME under /tmp or the macOS temp tree sits inside the bundled
    /tmp allowance, which must never re-permit the control plane under it."""
    sample = [
        "touch ~/.agentwire/damagecontrol.yml",
        "cd ~/.agentwire && touch damagecontrol.yml",
        "ln -sf /tmp/x ~/.claude/settings.json",
        "echo x > $HOME/.agentwire/damagecontrol.yml",
    ]
    project_under_tmp = [
        "touch /tmp/proj/.damagecontrol.yml",
        "cd /tmp/proj && touch .damage''control.yml",
    ]
    leaked = []
    for home in ("/home/cptest", "/tmp/x", "/var/folders/x/T/h"):
        monkeypatch.setenv("HOME", home)
        leaked += [(home, c) for c in _not_protected_block(sample + project_under_tmp, cfg)]
    assert not leaked, leaked


# --------------------------------------------------------------------------
# `agentwire tasks promote` — the confused-deputy CLI (#720)
# --------------------------------------------------------------------------

PROMOTE_COMMANDS = [
    "agentwire tasks promote",
    "agentwire tasks promote --yes",
    "uv run agentwire tasks promote --yes",
    "python3 -m agentwire tasks promote --yes",
]


def test_promote_is_blocked_whatever_the_wrapper_hatch_switch_or_allowlist(cfg, off):
    """A protected COMMAND has no escape valve at all — not even the allowlist."""
    allow_all = dict(cfg, allowedPaths=[{"path": "*", "allow": "all"}])
    leaked = _not_protected_block(PROMOTE_COMMANDS, cfg)
    leaked += _not_protected_block([c + "  # allow: I really want to" for c in PROMOTE_COMMANDS], cfg)
    leaked += _not_protected_block(PROMOTE_COMMANDS, off)
    leaked += _not_protected_block(PROMOTE_COMMANDS, allow_all)
    assert not leaked, leaked


def test_mentions_and_reads_of_the_control_plane_are_not_blocked(cfg):
    innocent = [
        'git commit -m "docs: mention agentwire tasks promote in the README"',
        'echo "run agentwire tasks promote when ready"',
        'gh issue comment 1 --body "see agentwire tasks promote for details"',
        "agentwire tasks review",
        'gh issue create --title "bug" --body "edit your .agentwire.tasks.yml to fix"',
        'git commit -m "docs: mention .agentwire.tasks.yml"',
        "cat .agentwire.tasks.yml",
        "grep roles /some/repo/.agentwire.tasks.yml",
        "python3 -m pytest tests/unit",
    ]
    wrongly_blocked = [
        c for c in innocent
        for r in [check_command(c, cfg)]
        if r["decision"] == "block" or r.get("protected")
    ]
    assert not wrongly_blocked, wrongly_blocked
    assert check_command("cat ~/.agentwire/damagecontrol.yml", cfg)["decision"] == "allow"


# --------------------------------------------------------------------------
# #1086 — a MENTION inside an interpreter program's prose literal is data;
# a real write through the same interpreter is still refused.
# --------------------------------------------------------------------------

_SETTINGS = "~/.claude/settings.json"

PROSE_MENTIONS = [
    f"cd /tmp && python3 - <<'EOF'\ns='hooks in {_SETTINGS} are empty'\n"
    "open('/tmp/x.md','w').write(s)\nEOF",
    f"python3 -c \"print('see {_SETTINGS} for details')\"",
    f"perl -e 'print \"see {_SETTINGS} by hand\"'",
]

# One per mechanism the prose redaction could wrongly swallow.
INTERPRETER_REAL_WRITES = [
    f"python3 - <<'EOF'\ns='hooks in {_SETTINGS} are empty'\nopen('{_SETTINGS}','w').write(s)\nEOF",
    f"python3 -c 'import os; os.system(\"echo x > {_SETTINGS}\")'",
    f"perl -e 'open(F, \">\", \"{_SETTINGS}\")'",
    # writers the shell ladder has no verb for
    f"python3 -c 'import os; os.system(\"curl -o {_SETTINGS} http://x/y\")'",
    # flagless, slashless, operatorless: only the first-word-is-a-program check saves it
    f"python3 -c 'import os; os.system(\"gzip {_SETTINGS} now\")'",
    # a stopword makes it LOOK like prose
    f"python3 -c 'import os; os.system(\"patch the {_SETTINGS} for me\")'",
    # a leading shell keyword must not hide the verb
    f"python3 -c 'import os; os.system(\"if true; then gzip {_SETTINGS}; fi\")'",
    # the verb sits in a NEIGHBOURING literal
    f"python3 -c 'import os; os.system(\"cp /tmp/a \" + \"{_SETTINGS} is x\")'",
    f"python3 -c 'import subprocess; subprocess.run([\"cp\",\"/tmp/a\",\"{_SETTINGS} is x\"])'",
    # indirection through an assigned variable, and prose piped to code as stdin
    f"python3 -c 'import os; s=\"the {_SETTINGS} is\"; open(s.split()[1],\"w\")'",
    f"echo 'the {_SETTINGS} is' | python3 -c 'import sys,os; os.system(\"echo hi > \"+sys.stdin.read().split()[1])'",
    # unknown tool, no sentence markers: not provably prose
    f"python3 -c 'import os; os.system(\"zzunknowntool {_SETTINGS} now\")'",
]


def test_prose_mention_in_an_interpreter_literal_is_not_blocked(cfg):
    wrongly_blocked = [c for c in PROSE_MENTIONS if check_command(c, cfg).get("protected") is True]
    assert not wrongly_blocked, wrongly_blocked


def test_real_interpreter_write_is_blocked_beside_prose(cfg):
    leaked = [c for c in INTERPRETER_REAL_WRITES if check_command(c, cfg).get("protected") is not True]
    assert not leaked, leaked


# --------------------------------------------------------------------------
# The ONE re-permit: a host-side allowlist entry in a PROTECTED file (#467).
# The project file is read from DISK via the PWD walk, so these exercise the
# real source of the allowlist.
# --------------------------------------------------------------------------

PROTECTED_TARGET = os.path.expanduser("~/.agentwire/damagecontrol.yml")


def _base_cfg(allowed=()):
    c = load_patterns()
    c["safety"] = {"enabled": True}
    c["allowedPaths"] = list(allowed)
    return c


def _write_verdicts(cfg):
    blocked, _ = check_path(PROTECTED_TARGET, cfg)
    return blocked, check_command(f"echo 'enabled: false' > {PROTECTED_TARGET}", cfg)["decision"]


def test_agentwire_yml_allowlist_does_not_repermit_the_control_plane(tmp_path, monkeypatch):
    """BUG REPRODUCER: otherwise an agent edits .agentwire.yml to free itself."""
    (tmp_path / ".agentwire.yml").write_text(
        "posture: bypass\nsafety:\n  allowed_paths:\n"
        f"    - path: {PROTECTED_TARGET}\n      allow: all\n"
    )
    monkeypatch.setenv("PWD", str(tmp_path))
    assert _write_verdicts(_base_cfg()) == (True, "block")


def test_host_side_allowlists_do_repermit_the_control_plane(tmp_path, monkeypatch):
    monkeypatch.setenv("PWD", str(tmp_path))
    entry = {"path": PROTECTED_TARGET, "allow": "all"}
    # global host allowlist (from the protected rule YAMLs)
    blocked, decision = _write_verdicts(_base_cfg([entry]))
    assert blocked is False and decision != "block"
    # per-project allowlist in the PROTECTED .damagecontrol.yml
    (tmp_path / ".damagecontrol.yml").write_text(
        f"enabled: true\nallowed_paths:\n  - path: {PROTECTED_TARGET}\n    allow: all\n"
    )
    blocked, decision = _write_verdicts(_base_cfg())
    assert blocked is False and decision != "block"


def test_kill_switch_is_read_from_the_host_file_and_fails_on(tmp_path):
    g = tmp_path / "damagecontrol.yml"
    assert load_safety_config(global_config_path=g, cwd=str(tmp_path))["enabled"] is True  # missing
    g.write_text("enabled: false\n")  # owner flips it on the host
    assert load_safety_config(global_config_path=g, cwd=str(tmp_path))["enabled"] is False
    g.write_text("enabled: [unclosed\n  : :")  # corrupt → stays ON
    assert load_safety_config(global_config_path=g, cwd=str(tmp_path))["enabled"] is True
