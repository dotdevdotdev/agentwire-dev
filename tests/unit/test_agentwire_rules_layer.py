"""The AgentWire rules layer split out of the generic files (#1209 M2).

Moving an entry between files must not change what is enforced, must not change what
heal installs on a machine that already has the old layout, and must keep the order
that makes AgentWire's MCP policies win over the generic write-shaped-name fallback.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentwire import safety_commands
from agentwire.safety import packs
from agentwire.safety.groups import DEFAULT_PACKS, REGISTRY, bundled_files, unclassified
from tests.unit.test_safety_packs import home  # noqa: F401  (fixture)

REPO = Path(__file__).resolve().parent.parent.parent
PKG = REPO / "agentwire"
AGENTWIRE_MCP_TOOLS = (
    "worktree_remove", "worktree_prune", "session_kill", "pane_kill", "session_recreate", "machine_remove",
)


def test_effective_rule_set_is_identical_to_the_pre_split_golden():
    """Sorted ids + patterns + tool policies + path lists, diffed against the set recorded
    BEFORE the split (scripts/effective_rules.py --ref <pre-split> --write)."""
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "effective_rules.py"), "--check"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_every_bundled_file_has_exactly_one_owner_and_the_owner_pins_still_hold():
    assert not unclassified(PKG)
    for name in ("control-plane", "core"):
        assert REGISTRY[name].kernel


def test_agentwire_mcp_policies_resolve_to_their_own_ids_not_the_writeish_fallback(mcp_hook):
    from agentwire.safety.groups import bundled_rule_dirs

    cfg = mcp_hook.load_config(bundled_rule_dirs(), PKG / "tooldefs")
    by_tool = {}
    for pol in cfg["mcpToolPolicies"]:  # first match wins
        for tool in AGENTWIRE_MCP_TOOLS:
            if pol.get("tool") == f"mcp__agentwire__{tool}":
                by_tool.setdefault(tool, pol["id"])
    assert by_tool == {t: "mcp.agentwire." + t.replace("_", "-") for t in AGENTWIRE_MCP_TOOLS}
    order = [p["id"] for p in cfg["mcpToolPolicies"]]
    assert max(order.index(i) for i in by_tool.values()) < order.index("mcp.writeish-name")


def test_the_owner_env_allow_is_still_loaded():
    from agentwire.safety._core import load_config
    from agentwire.safety.groups import bundled_rule_dirs

    allowed = [p["path"] for p in load_config(bundled_rule_dirs())["allowedPaths"] if isinstance(p, dict)]
    assert "~/projects/agentwire-dev/.env" in allowed and "~/.agentwire/.env" in allowed


@pytest.fixture
def machine(home):  # noqa: F811
    """An isolated ~/.agentwire that heal treats as canonical (fixture shared with the pack tests)."""
    safety_commands.RULES_DIR.mkdir(parents=True)
    return home


def _old_agentwire_yaml(monkeypatch) -> bytes:
    """Stand-in for a pre-carve install: the current file plus a marker, registered as shipped."""
    old = (safety_commands.get_damage_control_source() / "agentwire.yaml").read_bytes() + b"\n# pre-carve\n"
    real = safety_commands.load_rule_baselines()
    shipped = {**real, "rules": {**real["rules"], "agentwire.yaml": [hashlib.sha256(old).hexdigest()]}}
    monkeypatch.setattr(safety_commands, "load_rule_baselines", lambda: shipped)
    return old


def test_decided_machine_with_the_old_agentwire_file_keeps_its_tmux_rules(machine, monkeypatch):
    old = _old_agentwire_yaml(monkeypatch)
    (safety_commands.RULES_DIR / "agentwire.yaml").write_bytes(old)
    safety_commands.DAMAGECONTROL_FILE.write_text("enabled: true\npacks:\n  - aws\n")

    summary = safety_commands.heal_damage_control(quiet=True)

    assert summary["packs_migrated"] == list(DEFAULT_PACKS)
    assert set(packs.read_state(safety_commands.DAMAGECONTROL_FILE)["packs"]) == {"aws", "tmux"}
    assert (safety_commands.RULES_DIR / "tmux.yaml").exists()
    verdict = safety_commands.check_command_safety("tmux kill-server")
    assert verdict["decision"] != "allow"


def test_the_carry_forward_is_one_shot_so_disabling_tmux_afterwards_sticks(machine, monkeypatch):
    old = _old_agentwire_yaml(monkeypatch)
    (safety_commands.RULES_DIR / "agentwire.yaml").write_bytes(old)
    safety_commands.DAMAGECONTROL_FILE.write_text("enabled: true\npacks: []\n")
    safety_commands.heal_damage_control(quiet=True)  # records tmux, brings agentwire.yaml forward

    packs.write_state(safety_commands.DAMAGECONTROL_FILE, packs=[])
    summary = safety_commands.heal_damage_control(quiet=True)

    assert summary["packs_migrated"] == []
    assert packs.read_state(safety_commands.DAMAGECONTROL_FILE)["packs"] == []
    assert not (safety_commands.RULES_DIR / "tmux.yaml").exists()


def test_an_edited_agentwire_file_is_not_a_carry_forward_marker(machine, monkeypatch):
    _old_agentwire_yaml(monkeypatch)
    (safety_commands.RULES_DIR / "agentwire.yaml").write_text("# my own rules\nbashToolPatterns: []\n")
    safety_commands.DAMAGECONTROL_FILE.write_text("enabled: true\npacks: []\n")

    assert safety_commands.heal_damage_control(quiet=True)["packs_migrated"] == []


def test_heal_installs_both_bundled_dirs_flat(machine):
    safety_commands.heal_damage_control(quiet=True)
    wanted = packs.wanted_files("rules", packs.read_state(safety_commands.DAMAGECONTROL_FILE))
    installed = {p.name for p in safety_commands.RULES_DIR.glob("*.yaml")}
    assert wanted <= installed
    assert {"agentwire-core.yaml", "agentwire-mcp-tools.yaml", "agentwire.yaml"} <= installed
    assert installed <= bundled_files(PKG)["rules"]
    json.dumps(sorted(installed))  # stays a plain flat name set
