"""The five real hook entrypoints, driven as subprocesses with recorded payloads (#1209 M0).

Stdlib + pyyaml only, so the Python 3.9 CI job can run it without installing
agentwire: it asserts the exit code and stdout Claude Code actually sees (exit 2 =
block, ``permissionDecision: ask`` on stdout = confirm, exit 0 + silence = allow).
Full-corpus coverage of the same entrypoints lives in the golden snapshot; this file
is the small, fast, interpreter-portable slice.

A fixed fake HOME outside /tmp (core.yaml allowlists /tmp, which would flip every
``~`` secret case to allow) and a private AGENTWIRE_DIR keep it hermetic.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control"
FAKE_HOME = "/golden/home"
BASE = {"session_id": "00000000-0000-0000-0000-000000000000", "cwd": FAKE_HOME + "/project",
        "hook_event_name": "PreToolUse"}

# (hook, tool_name, tool_input, verdict) in the default permission mode.
INTERACTIVE = [
    ("bash", "Bash", {"command": "git status"}, "allow"),
    ("bash", "Bash", {"command": "git reset --hard HEAD~3"}, "block"),
    ("bash", "Bash", {"command": "echo hi > ~/.agentwire/damagecontrol.yml"}, "block"),
    ("bash", "Bash", {"command": "cat ~/.ssh/id_rsa"}, "block"),
    ("bash", "Bash", {"command": 'eval "$PAYLOAD"'}, "ask"),
    ("bash", "Bash", {"command": "docker compose up -d"}, "ask"),
    ("bash", "PowerShell", {"command": "git reset --hard HEAD~3"}, "block"),
    ("edit", "Edit", {"file_path": FAKE_HOME + "/.agentwire/damagecontrol.yml"}, "block"),
    ("edit", "Edit", {"file_path": FAKE_HOME + "/project/main.py"}, "allow"),
    ("write", "Write", {"file_path": FAKE_HOME + "/.claude/settings.json"}, "block"),
    ("read", "Read", {"file_path": FAKE_HOME + "/.ssh/id_rsa"}, "block"),
    ("read", "Read", {"file_path": FAKE_HOME + "/project/main.py"}, "allow"),
    ("mcp", "mcp__agentwire__session_kill", {}, "ask"),
    ("mcp", "mcp__agentwire__sessions_list", {}, "allow"),
]


def _run(hook, tool, tool_input, tmp_path, mode="default", unattended=False):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": FAKE_HOME,
           "PWD": FAKE_HOME + "/project", "AGENTWIRE_DIR": str(tmp_path)}
    if unattended:
        env["AGENTWIRE_UNATTENDED"] = "1"
    payload = dict(BASE, tool_name=tool, tool_input=tool_input, permission_mode=mode)
    proc = subprocess.run(
        [sys.executable, str(HOOKS_DIR / (hook + "-tool-damage-control.py"))],
        input=json.dumps(payload), capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    if proc.returncode == 2:
        return "block"
    if proc.returncode != 0:
        return "crash-%d: %s" % (proc.returncode, proc.stderr[-200:])
    if proc.stdout.strip():
        return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
    return "allow"


def test_each_hook_returns_the_verdict_claude_code_acts_on(tmp_path):
    bad = [
        (hook, tool, tool_input, want, got)
        for hook, tool, tool_input, want in INTERACTIVE
        for got in [_run(hook, tool, tool_input, tmp_path)]
        if got != want
    ]
    assert not bad, bad


def test_bypass_demotes_an_ask_but_never_a_verb_concealing_one(tmp_path):
    """bypassPermissions means 'do not stop me for what you recognise', not 'for what you cannot see'."""
    demoted = _run("bash", "Bash", {"command": "docker compose up -d"}, tmp_path, mode="bypassPermissions")
    concealed = _run("bash", "Bash", {"command": 'eval "$PAYLOAD"'}, tmp_path, mode="bypassPermissions")
    blocked = _run("bash", "Bash", {"command": "git reset --hard HEAD~3"}, tmp_path, mode="bypassPermissions")
    assert (demoted, concealed, blocked) == ("allow", "ask", "block")


def test_unattended_fails_closed_on_an_ask(tmp_path):
    got = _run("bash", "Bash", {"command": "docker compose up -d"}, tmp_path, unattended=True)
    assert got == "block"
