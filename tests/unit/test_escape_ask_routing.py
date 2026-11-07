"""Escape-asks (`# ask: <reason>`) routing, end to end (#1278).

An agent that expects a block ends the command with `# ask: <reason>`; a human
must decide. Role prompts no longer teach the syntax (#1315); the engine and the
permission hook still honour it for anyone who uses it: the hook never
auto-approves it under bypass/auto, and a child's ask routes to its parent / the
owner with the reason visible.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agentwire import prompt_router
from agentwire.roles import load_roles, merge_roles, resolve_roles

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


def test_permission_hook_never_auto_approves_in_any_mode(tmp_path):
    """A request that reaches the hook goes to the portal in EVERY mode; bypass/auto never short-circuit to allow."""
    home = tmp_path / "home"
    project = home / "projects" / "demo"
    project.mkdir(parents=True)
    stub = tmp_path / "stub"
    stub.mkdir()
    log = tmp_path / "portal.log"
    curl = stub / "curl"
    curl.write_text('#!/usr/bin/env bash\necho posted >> "$STUBLOG"\necho \'{"decision":"deny","message":"stub"}\'\n')
    curl.chmod(0o755)

    got = {}
    for mode in ("bypassPermissions", "auto", "default"):
        log.write_text("")
        payload = json.dumps({"tool_name": "Bash", "permission_mode": mode, "tool_input": {"command": ESCAPE}})
        env = {**os.environ, "HOME": str(home), "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
               "STUBLOG": str(log)}
        env.pop("TMUX", None)
        proc = subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True,
                              env=env, cwd=project, timeout=30)
        got[mode] = ('"decision":"allow"' in proc.stdout, log.read_text().count("posted"))
    assert got == {m: (False, 1) for m in ("bypassPermissions", "auto", "default")}


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
