"""Global kill switch (safety.enabled = false) short-circuits all blocks."""

from agentwire.safety._core import check_command, check_path
from agentwire.safety_commands import load_patterns


def test_kill_switch_allows_dangerous_bash():
    cfg = load_patterns()
    cfg["safety"] = {"enabled": False, "disabled_rules": []}
    result = check_command("rm -rf /", cfg)
    assert result["decision"] == "allow"
    assert result.get("disabled") is True
    assert result["reason"] == "safety disabled"


def test_kill_switch_allows_protected_file():
    cfg = load_patterns()
    cfg["safety"] = {"enabled": False, "disabled_rules": []}
    blocked, _reason = check_path("/etc/passwd", cfg)
    assert blocked is False


def test_default_enabled_blocks():
    cfg = load_patterns()
    # No explicit safety key: defaults to enabled=True
    result = check_command("rm -rf /", cfg)
    assert result["decision"] == "block"


def test_explicit_enabled_true_blocks():
    cfg = load_patterns()
    cfg["safety"] = {"enabled": True, "disabled_rules": []}
    result = check_command("rm -rf /", cfg)
    assert result["decision"] == "block"
