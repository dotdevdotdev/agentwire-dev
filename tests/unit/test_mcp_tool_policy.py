"""#923 remainder — per-tool policy for MCP tool calls.

RULE SET UNDER TEST: the BUNDLED rules + BUNDLED tooldefs (what a hermetic CI
checkout ships), never this machine's live ``~/.agentwire`` config (#916).
The hook-level behaviour of these tiers (ask / bypass demote / unattended
block / grants) is driven through the real hook in test_damage_control_hooks.
"""

from pathlib import Path

import pytest

from agentwire.safety.groups import bundled_rule_dirs

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = bundled_rule_dirs()
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"

DANGEROUS_CORE = {
    "mcp__agentwire__worktree_remove": "mcp.agentwire.worktree-remove",
    "mcp__agentwire__worktree_prune": "mcp.agentwire.worktree-prune",
    "mcp__agentwire__session_kill": "mcp.agentwire.session-kill",
    "mcp__agentwire__pane_kill": "mcp.agentwire.pane-kill",
    "mcp__agentwire__session_recreate": "mcp.agentwire.session-recreate",
    "mcp__agentwire__machine_remove": "mcp.agentwire.machine-remove",
}

# Messaging stays ungated BY DECISION (its Bash twin is unruled); read/additive
# tools fall through unmatched.
OPEN_TOOLS = [
    "mcp__agentwire__msg_send",
    "mcp__agentwire__session_send",
    "mcp__agentwire__notify_parent",
    "mcp__agentwire__sessions_list",
    "mcp__filesystem__read_file",
]


@pytest.fixture(scope="module")
def bundled_config(mcp_hook):
    cfg = mcp_hook.load_config(RULES_DIR, TOOLDEFS_DIR)
    assert not cfg.get("_parser_unavailable"), "rules failed to load"
    cfg["safety"] = {"enabled": True}
    return cfg


def test_every_teardown_and_kill_tool_is_ask_tier(mcp_hook, bundled_config):
    bad = {
        tool: (r["decision"], r.get("id"))
        for tool, rule_id in DANGEROUS_CORE.items()
        for r in [mcp_hook.check_mcp_tool(tool, bundled_config)]
        if not (r["decision"] == "ask" and r.get("id") == rule_id)
    }
    assert not bad, bad


def test_messaging_and_read_tools_are_not_gated(mcp_hook, bundled_config):
    bad = [t for t in OPEN_TOOLS if mcp_hook.check_mcp_tool(t, bundled_config)["decision"] != "allow"]
    assert not bad, bad
