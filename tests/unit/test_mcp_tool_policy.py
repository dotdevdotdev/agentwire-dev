"""#923 remainder — per-tool policy for MCP tool calls (option 1).

RULE SET UNDER TEST: the BUNDLED rules + BUNDLED tooldefs (what a hermetic CI
checkout ships), never this machine's live ``~/.agentwire`` config — a pin
that encodes the live environment goes red in CI (#916).
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import HOOKS_DIR

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"

# Deliberately not a real HOME on any runner — the hooks match strings, they
# never stat (see test_safety_coverage_wave.HERMETIC_HOME for the /tmp trap).
HERMETIC_HOME = "/home/agentwire-hermetic"

DANGEROUS_CORE = {
    "mcp__agentwire__worktree_remove": "mcp.agentwire.worktree-remove",
    "mcp__agentwire__worktree_prune": "mcp.agentwire.worktree-prune",
    "mcp__agentwire__session_kill": "mcp.agentwire.session-kill",
    "mcp__agentwire__pane_kill": "mcp.agentwire.pane-kill",
    "mcp__agentwire__session_recreate": "mcp.agentwire.session-recreate",
    "mcp__agentwire__machine_remove": "mcp.agentwire.machine-remove",
}


@pytest.fixture(scope="module")
def bundled_config(mcp_hook):
    cfg = mcp_hook.load_config(RULES_DIR, TOOLDEFS_DIR)
    assert not cfg.get("_parser_unavailable"), "rules failed to load"
    cfg["safety"] = {"enabled": True}
    return cfg


# ---------------------------------------------------------------------------
# In-process: check_mcp_tool over the bundled policy set
# ---------------------------------------------------------------------------


class TestPolicyClassification:
    def test_policies_loaded_with_no_duplicate_ids(self, bundled_config):
        assert bundled_config.get("mcpToolPolicies"), "bundled mcpToolPolicies missing"
        assert "_duplicate_rule_ids" not in bundled_config

    @pytest.mark.parametrize("tool,rule_id", sorted(DANGEROUS_CORE.items()))
    def test_dangerous_core_is_ask_tier(self, mcp_hook, bundled_config, tool, rule_id):
        result = mcp_hook.check_mcp_tool(tool, bundled_config)
        assert result["decision"] == "ask"
        assert result["id"] == rule_id
        assert result["mutates"] is True

    @pytest.mark.parametrize("tool", [
        # Messaging tools stay ungated by design: their effect is a message to
        # a Claude session running its own damage control, and the Bash twin
        # (`agentwire send`) is unruled — gating only the tool would recreate
        # #923's channel asymmetry in reverse.
        "mcp__agentwire__msg_send",
        "mcp__agentwire__session_send",
        "mcp__agentwire__notify_parent",
        # Read/additive tools fall through unmatched.
        "mcp__agentwire__sessions_list",
        "mcp__agentwire__worktree_status",
        "mcp__filesystem__read_file",
    ])
    def test_unclassified_tools_allow(self, mcp_hook, bundled_config, tool):
        result = mcp_hook.check_mcp_tool(tool, bundled_config)
        assert result["decision"] == "allow"

    def test_writeish_fallback_is_data(self, mcp_hook, bundled_config):
        """The #1036 name-shape heuristic now lives as the mcp.writeish-name
        entry — an unclassified write-shaped tool is allow-tier but mutating."""
        result = mcp_hook.check_mcp_tool("mcp__filesystem__write_file", bundled_config)
        assert result["decision"] == "allow"
        assert result["id"] == "mcp.writeish-name"
        assert result["mutates"] is True

    def test_read_shaped_tool_is_unmatched(self, mcp_hook, bundled_config):
        result = mcp_hook.check_mcp_tool("mcp__filesystem__read_file", bundled_config)
        assert result["matched"] is False
        assert result["mutates"] is None

    def test_disabled_rule_falls_through_to_fallback(self, mcp_hook, bundled_config):
        """disabled_rules can name a policy id; the tool then falls through to
        later entries (here the writeish fallback, since the name says remove)."""
        cfg = dict(bundled_config)
        cfg["safety"] = {"enabled": True,
                         "disabled_rules": ["mcp.agentwire.worktree-remove"]}
        result = mcp_hook.check_mcp_tool("mcp__agentwire__worktree_remove", cfg)
        assert result["decision"] == "allow"
        assert result["id"] == "mcp.writeish-name"

    def test_kill_switch_allows(self, mcp_hook, bundled_config):
        cfg = dict(bundled_config)
        cfg["safety"] = {"enabled": False}
        result = mcp_hook.check_mcp_tool("mcp__agentwire__worktree_remove", cfg)
        assert result["decision"] == "allow"
        assert result.get("disabled") is True

    def test_parser_unavailable_fails_closed(self, mcp_hook, bundled_config):
        cfg = dict(bundled_config)
        cfg["_parser_unavailable"] = "pyyaml unavailable — cannot load rules"
        assert mcp_hook.check_mcp_tool("mcp__anything__x", cfg)["decision"] == "block"

    def test_classification_is_data_not_name_coded(self, mcp_hook, bundled_config):
        """Mutation control: strip the policies and the ask tier disappears —
        proving the tier comes from the rule file, not a hardcoded name list."""
        cfg = dict(bundled_config)
        cfg["mcpToolPolicies"] = []
        result = mcp_hook.check_mcp_tool("mcp__agentwire__session_kill", cfg)
        assert result["decision"] == "allow"
        assert result["matched"] is False

    def test_grant_ids_are_known_to_the_lint(self, bundled_config):
        """A task granting a policy id must not be warned as unknown (#916's
        silently-inert-grant shape)."""
        from agentwire.safety.lint import lint_task_posture

        class _Task:
            unattended_allow = list(DANGEROUS_CORE.values())
            prompt = ""
            pre = None
            post = None
            shell = None
            on_task_end = None

        report = lint_task_posture(_Task(), bundled_config, cwd="/")
        assert report.unknown_grants == []


# ---------------------------------------------------------------------------
# Subprocess: the deployed hook file end-to-end (bundled rules, hermetic HOME)
# ---------------------------------------------------------------------------


class TestMcpPolicyHook:
    HOOK = HOOKS_DIR / "mcp-tool-damage-control.py"

    def _run(self, tool_name, tool_input, tmp_path, *, permission_mode="bypassPermissions",
             unattended=False, allow=None):
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": HERMETIC_HOME,
            "AGENTWIRE_DIR": str(tmp_path / ".agentwire"),
        }
        if unattended:
            env["AGENTWIRE_UNATTENDED"] = "1"
        if allow is not None:
            env["AGENTWIRE_UNATTENDED_ALLOW"] = allow
        payload = {"tool_name": tool_name, "tool_input": tool_input,
                   "permission_mode": permission_mode}
        return subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True, text=True, env=env, timeout=15,
        )

    def test_unattended_teardown_blocks(self, tmp_path):
        proc = self._run("mcp__agentwire__worktree_remove", {"name": "feature-x"},
                         tmp_path, unattended=True)
        assert proc.returncode == 2, proc.stderr
        assert "unattended" in proc.stderr
        assert "mcp.agentwire.worktree-remove" in proc.stderr

    def test_unattended_kill_blocks(self, tmp_path):
        proc = self._run("mcp__agentwire__session_kill", {"session": "x"},
                         tmp_path, unattended=True)
        assert proc.returncode == 2, proc.stderr

    def test_unattended_grant_allows(self, tmp_path):
        proc = self._run("mcp__agentwire__worktree_remove", {"name": "feature-x"},
                         tmp_path, unattended=True,
                         allow="mcp.agentwire.worktree-remove")
        assert proc.returncode == 0, proc.stderr

    def test_scoped_grant_refuses_a_tool_call(self, tmp_path):
        """A tool call has no filesystem target — a path-scoped grant refuses
        rather than measuring the scope against the cwd (#914)."""
        scoped = json.dumps([{"id": "mcp.agentwire.worktree-remove",
                              "paths": ["/srv/repos/"]}])
        proc = self._run("mcp__agentwire__worktree_remove", {"name": "feature-x"},
                         tmp_path, unattended=True, allow=scoped)
        assert proc.returncode == 2, proc.stderr

    def test_unattended_messaging_stays_open(self, tmp_path):
        """The priced false-refusal half: report-backs must never dead-loop an
        unattended session."""
        proc = self._run("mcp__agentwire__msg_send",
                         {"to": "orch", "kind": "done", "message": "finished"},
                         tmp_path, unattended=True)
        assert proc.returncode == 0, proc.stderr

    def test_attended_confirm_emits_ask(self, tmp_path):
        proc = self._run("mcp__agentwire__worktree_remove", {"name": "feature-x"},
                         tmp_path, permission_mode="default")
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["hookSpecificOutput"]["permissionDecision"] == "ask"

    def test_attended_bypass_demotes_to_allow(self, tmp_path):
        proc = self._run("mcp__agentwire__worktree_remove", {"name": "feature-x"},
                         tmp_path, permission_mode="bypassPermissions")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == ""

    def test_stale_rules_dir_bridge_keeps_control_plane_screening(self, tmp_path):
        """A user rules dir predating mcp-tools.yaml loads zero policies; the
        in-hook bridge must keep #1036's write-shaped control-plane screen."""
        user_rules = tmp_path / ".agentwire" / "damage-control"
        user_rules.mkdir(parents=True)
        for f in RULES_DIR.glob("*.yaml"):
            if f.name != "mcp-tools.yaml":
                shutil.copy(f, user_rules / f.name)
        proc = self._run("mcp__filesystem__write_file",
                         {"path": HERMETIC_HOME + "/.claude/settings.json", "content": "x"},
                         tmp_path)
        assert proc.returncode == 2, proc.stderr
        # And the teardown tier is honestly ABSENT on such an install (heal
        # brings mcp-tools.yaml forward) — the bridge covers paths, not tiers.
        proc = self._run("mcp__agentwire__worktree_remove", {"name": "x"},
                         tmp_path, unattended=True)
        assert proc.returncode == 0, proc.stderr
