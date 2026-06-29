"""Escape hatch: `# allow: <reason>` makes the hook skip pattern checks and log it."""

from agentwire.safety_commands import load_patterns
from agentwire.safety._core import check_command, detect_escape_hatch


def test_detect_escape_hatch_basic():
    assert detect_escape_hatch("foo  # allow: cleanup") == "cleanup"


def test_detect_escape_hatch_no_reason_returns_none():
    assert detect_escape_hatch("foo  # allow:  ") is None


def test_detect_escape_hatch_absent():
    assert detect_escape_hatch("rm -rf /tmp") is None


def test_check_command_escape_hatch_overrides_block():
    cfg = load_patterns()
    result = check_command("rm -rf /tmp  # allow: build cleanup", cfg)
    assert result["decision"] == "allow"
    assert result["escape"] is True
    assert result["escape_reason"] == "build cleanup"
    assert "Escape hatch" in result["reason"]


def test_check_command_no_escape_still_blocks():
    cfg = load_patterns()
    result = check_command("rm -rf /tmp", cfg)
    assert result["decision"] == "block"
    assert "rm" in result["reason"].lower()


def test_escape_hatch_inline_comment_at_end():
    cfg = load_patterns()
    result = check_command("git reset --hard origin/main  # allow: sync to remote", cfg)
    assert result["decision"] == "allow"
    assert result["escape_reason"] == "sync to remote"
