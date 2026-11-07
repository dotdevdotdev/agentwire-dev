"""Escape-asks (`# ask: <reason>`, damage-control-dev 0.2.0) end to end (#1278).

An agent that expects a block ends the command with `# ask: <reason>`; a human
must decide. Role prompts no longer teach the syntax (#1315); the engine and the
permission hook still honour it for anyone who uses it: the hook never
auto-approves it under bypass/auto, and a child's ask routes to its parent / the
owner with the reason visible.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentwire import prompt_router
from agentwire.roles import load_roles, merge_roles, resolve_roles
from tests.unit.test_hook_subprocess import _run

BLOCKED = "git reset --hard HEAD~3"
ESCAPE = f"{BLOCKED} # ask: need to drop the bad rebase, branch is pushed"
HOOK = Path(prompt_router.__file__).parent / "hooks" / "agentwire-permission.sh"


def test_no_role_teaches_the_escape_ask():
    taught = []
    for kind in ("worker", "reviewer", "orchestrator"):
        for worktree in (False, True):
            roles, absent = load_roles(resolve_roles(kind, worktree_topology=worktree))
            assert not absent
            if "# ask:" in merge_roles(roles).instructions:
                taught.append((kind, worktree))
    assert not taught, taught


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
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path), "PWD": str(tmp_path),
        "AGENTWIRE_BIN": str(Path(sys.executable).parent / "agentwire"),
    }
    return subprocess.run(
        ["bash", str(HOOK)], input=payload, capture_output=True, text=True,
        env=env, cwd=tmp_path, timeout=30,
    )


def test_permission_hook_never_auto_approves_a_non_demotable_ask(tmp_path):
    # Not in tmux and outside ~/projects: anything past the bypass short-circuit
    # dies on "could not determine session" before touching the network.
    cases = {
        "plain": (BLOCKED, True),
        "demotable-ask": ("docker compose up -d", True),
        "escape": (ESCAPE, False),
        "unverifiable": ('eval "$PAYLOAD"', False),
    }
    got = {}
    for label, (cmd, _) in cases.items():
        for mode in ("bypassPermissions", "auto"):
            got[f"{label}/{mode}"] = '"decision":"allow"' in _hook(cmd, mode, tmp_path).stdout
    want = {f"{label}/{mode}": allowed for label, (_, allowed) in cases.items()
            for mode in ("bypassPermissions", "auto")}
    assert got == want


def test_permission_hook_holds_for_a_human_when_the_engine_check_is_unavailable(tmp_path):
    payload = '{"tool_name":"Bash","permission_mode":"bypassPermissions","tool_input":{"command":"ls"}}'
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PWD": str(tmp_path),
           "AGENTWIRE_BIN": str(tmp_path / "missing")}
    proc = subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=30)
    assert '"decision":"allow"' not in proc.stdout


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


def test_routed_command_excerpt_keeps_head_and_tail_and_never_exceeds_the_cap():
    cap = prompt_router.COMMAND_EXCERPT_HEAD + prompt_router.COMMAND_EXCERPT_TAIL + 3
    short = "ls -la"
    long_cmd = "START" + "x" * 800 + "END"
    out = prompt_router._command_excerpt(long_cmd)
    bad = []
    if prompt_router._command_excerpt(short) != short:
        bad.append("short command altered")
    if not (out.startswith("START") and out.endswith("END")):
        bad.append("head/tail lost")
    if len(out) > cap:
        bad.append(f"too long: {len(out)}")
    assert not bad, bad
