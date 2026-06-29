"""Disabled rules in safety config skip the matching pattern."""

from agentwire.safety_commands import load_patterns
from agentwire.safety._core import check_command


def test_rule_ids_are_populated():
    cfg = load_patterns()
    bash_patterns = cfg.get("bashToolPatterns", [])
    assert bash_patterns, "expected at least one bash pattern"
    for p in bash_patterns:
        assert "id" in p, f"missing id on rule: {p.get('reason')}"
        assert isinstance(p["id"], str) and "." in p["id"]


def test_disabled_rule_skips_block():
    cfg = load_patterns()
    cfg["safety"] = {"enabled": True, "disabled_rules": ["core.filesystem-format-command"]}
    result = check_command("mkfs.ext4 /dev/sda1", cfg)
    assert result["decision"] == "allow"


def test_non_disabled_rule_still_blocks():
    cfg = load_patterns()
    cfg["safety"] = {"enabled": True, "disabled_rules": ["nonexistent.rule"]}
    result = check_command("mkfs.ext4 /dev/sda1", cfg)
    assert result["decision"] == "block"
    assert result.get("id") == "core.filesystem-format-command"


def test_disabled_rule_only_affects_named_rule():
    cfg = load_patterns()
    cfg["safety"] = {"enabled": True, "disabled_rules": ["core.filesystem-format-command"]}
    # A different blocking rule still fires
    result = check_command("git filter-branch --tree-filter rm 'foo'", cfg)
    assert result["decision"] == "block"
