"""The unattended allowlist grants an OPERATION and shadows no hard block (#925).

Built from BUNDLED rules + BUNDLED tooldefs: the live copies under
``~/.agentwire/`` drift, and tuning against semantics that do not ship is how a
fix lands backwards. Verdicts go through the SHIPPING resolver
(``resolve_unattended_grants`` / ``unattended_grant_allows``), and each case
pins the rule id it reached, because an earlier ``ask`` rule that hides a later
``block`` is exactly the hole a grant would open.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentwire.safety import _core as C  # noqa: N812

REPO = Path(__file__).resolve().parent.parent.parent
BUNDLED_RULES = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
BUNDLED_TOOLDEFS = REPO / "agentwire" / "tooldefs"

UV_IDS = {
    "tooldef.uv-run-a-script-in-project-environment",
    "tooldef.uv-run-a-command-in-project-environment",
}

# Keep the literal out of this file's own text where it would be scanned as a
# command by the very hooks under test (#915).
RM = "r" + "m"


@pytest.fixture(scope="module")
def cfg():
    config = C.load_config(BUNDLED_RULES, BUNDLED_TOOLDEFS)
    assert sum(1 for p in config.get("bashToolPatterns", []) if p.get("anchored")) > 100, (
        "tooldefs did not load — every verdict below would read ALLOW and mean nothing")
    config["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return config


def unattended_verdict(cfg, command, cwd="/work/repo"):
    r = C.check_command(command, cfg)
    if r["decision"] != "ask":
        return r["decision"], r.get("id")
    granted, _why = C.unattended_grant_allows(
        r["id"], command, C.resolve_unattended_grants(cfg), cwd, pattern=None)
    return ("allow" if granted else "block"), r["id"]


def test_every_default_grant_names_a_real_ask_tier_rule(cfg):
    """A grant naming nothing is a permission that does nothing (five of six were
    inert on the owner's machine, 2026-08-06); one naming a block is misleading."""
    by_id = {p["id"]: p for p in cfg["bashToolPatterns"] if p.get("id")}
    bad = [
        rule_id for rule_id in C.parse_unattended_allow(C.DEFAULT_UNATTENDED_ALLOW)[0]
        if rule_id not in by_id or not (by_id[rule_id].get("ask") or by_id[rule_id].get("bypassable"))
    ]
    assert not bad, bad


def test_a_scheduled_task_can_run_its_own_tooling(cfg):
    """A task that cannot run `uv run` cannot verify its own work."""
    bad = []
    for command in ("uv run amo status 2>&1", "uv run --extra dev pytest tests/unit -q", "uv run ruff check ."):
        decision, rule_id = unattended_verdict(cfg, command)
        if rule_id not in UV_IDS or decision != "allow":
            bad.append((command, decision, rule_id))
    assert not bad, bad


def test_a_launcher_prefix_never_turns_a_hard_block_into_a_grant(cfg):
    """`uv run` is granted; what it launches must still be judged as itself."""
    bad = []
    for prefix in ("uv run ", "env ", "nohup ", "xargs -I{} "):
        for command in (f"{RM} -rf /tmp/x", "git push --force origin main"):
            for spelling in (f"{prefix}{command}", f"{prefix}bash -c '{command}'"):
                decision, rule_id = unattended_verdict(cfg, spelling)
                if decision != "block":
                    bad.append((spelling, decision, rule_id))
    assert not bad, bad
