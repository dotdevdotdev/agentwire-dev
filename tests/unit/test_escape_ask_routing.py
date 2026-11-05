"""Escape-asks (`# ask: <reason>`, damage-control-dev 0.2.0) end to end (#1278).

An agent that expects a block ends the command with `# ask: <reason>`; a human
must decide. That only works if (1) agents know the syntax, (2) the permission
hook never auto-approves it under bypass/auto, and (3) a child's ask routes to
its parent / the owner with the reason visible.
"""

import os
import subprocess
from pathlib import Path

import pytest

from agentwire import prompt_router
from agentwire.roles import load_roles, merge_roles, resolve_roles
from tests.unit.test_hook_subprocess import _run

BLOCKED = "git reset --hard HEAD~3"
ESCAPE = f"{BLOCKED} # ask: need to drop the bad rebase, branch is pushed"
HOOK = Path(prompt_router.__file__).parent / "hooks" / "agentwire-permission.sh"


def test_every_role_that_runs_commands_teaches_the_escape_ask():
    missing = []
    for kind in ("worker", "reviewer"):
        for worktree in (False, True):
            names = resolve_roles(kind, worktree_topology=worktree)
            roles, absent = load_roles(names)
            assert not absent
            if "# ask:" not in merge_roles(roles).instructions:
                missing.append((kind, worktree))
    roles, _ = load_roles(resolve_roles("orchestrator"))
    if "# ask:" not in merge_roles(roles).instructions:
        missing.append(("orchestrator", False))
    assert not missing, missing


def test_engine_asks_under_bypass_and_blocks_when_unattended(tmp_path):
    got = {
        "plain": _run("bash", "Bash", {"command": BLOCKED}, tmp_path, mode="bypassPermissions"),
        "escape": _run("bash", "Bash", {"command": ESCAPE}, tmp_path, mode="bypassPermissions"),
        "unattended": _run("bash", "Bash", {"command": ESCAPE}, tmp_path, unattended=True),
    }
    assert got == {"plain": "block", "escape": "ask", "unattended": "block"}


def _hook(command: str, mode: str, tmp_path: Path) -> subprocess.CompletedProcess:
    payload = '{"tool_name":"Bash","permission_mode":"%s","tool_input":{"command":%s}}' % (
        mode, __import__("json").dumps(command),
    )
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PWD": str(tmp_path)}
    return subprocess.run(
        ["bash", str(HOOK)], input=payload, capture_output=True, text=True,
        env=env, cwd=tmp_path, timeout=30,
    )


def test_permission_hook_never_auto_approves_an_escape_ask(tmp_path):
    # Not in tmux and outside ~/projects: anything past the bypass short-circuit
    # dies on "could not determine session" before touching the network.
    got = {}
    for label, cmd in (("plain", BLOCKED), ("escape", ESCAPE)):
        proc = _hook(cmd, "bypassPermissions", tmp_path)
        got[label] = '"decision":"allow"' in proc.stdout
    assert got == {"plain": True, "escape": False}


@pytest.fixture
def routed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        prompt_router, "route_prompt",
        lambda s, p, info, source="hook", project_path=None: calls.append(info) or "orch",
    )
    return calls


def test_escape_ask_routes_with_the_command_and_the_agents_reason(routed):
    prompt_router.notify_permission_request(
        "child", 0, {"tool_name": "Bash", "tool_input": {"command": ESCAPE}}
    )
    (info,) = routed
    assert info.kind == "permission"
    assert BLOCKED in info.question and "branch is pushed" in info.question


def test_long_command_cannot_truncate_the_reason_away(routed):
    long_cmd = f"echo {'x' * 600}; {BLOCKED} # ask: reason must survive"
    prompt_router.notify_permission_request(
        "child", 0, {"tool_name": "Bash", "tool_input": {"command": long_cmd}}
    )
    assert "reason must survive" in routed[0].question


def test_root_session_escape_ask_escalates_to_the_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path)
    monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
    escalated = []
    monkeypatch.setattr(
        prompt_router, "_escalate_no_parent",
        lambda session, pane, info, prior: escalated.append(info) or "now",
    )
    prompt_router.notify_permission_request(
        "root", 0, {"tool_name": "Bash", "tool_input": {"command": ESCAPE}}
    )
    (info,) = escalated
    assert "branch is pushed" in info.question
