"""Tests for session context-bar parsing (issue #442, Phase 0 — observe only).

Locks in the verified semantic: the Claude Code footer bar percentage is
context REMAINING (headroom before auto-compact), not usage — so a LOW number
means a bloated session. Parsing is pure string work on captured pane text;
these tests cover the real bar shapes seen on live sessions plus the daemon /
no-bar skip paths.
"""

from unittest.mock import patch

from agentwire import session_context as sc

# Real captures from live sessions (2026-06-22): the bar width tracks terminal
# width, so the same 92% renders with different glyph counts.
WIDE_BAR = "  [████████████████████████████████████████████████████████████████████████████████░░░░░░░░] 92%"
NARROW_BAR = "  [████████████████████████████████████████████████████████████████░░░░░░] 92%"
LOW_BAR = "  [█████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 12%"
META_LINE = "  ~/projects/agentwire-dev  main                       opus  $1805.19  7988m"


def test_parse_bar_wide_and_narrow_same_pct():
    assert sc.parse_context_bar(WIDE_BAR) == 92
    assert sc.parse_context_bar(NARROW_BAR) == 92


def test_parse_bar_low():
    assert sc.parse_context_bar(LOW_BAR) == 12


def test_parse_bar_with_ansi():
    noisy = "\x1b[2m" + WIDE_BAR + "\x1b[0m"
    assert sc.parse_context_bar(noisy) == 92


def test_no_bar_returns_none():
    assert sc.parse_context_bar("INFO: 127.0.0.1 - GET /health 200 OK") is None
    assert sc.parse_context_bar("[2026-06-22 07:43:41] Nothing due. Sleeping 0s...") is None
    assert sc.parse_context_bar("") is None


def test_out_of_range_rejected():
    assert sc.parse_context_bar("[██░░] 150%") is None


def test_parse_model():
    assert sc.parse_model(META_LINE) == "opus"
    assert sc.parse_model("  main  sonnet  $1.00") == "sonnet"
    assert sc.parse_model("no model here") is None


def _ctx(command: str, pane_text: str):
    """Build a SessionContext with pane command + capture mocked."""
    with patch.object(sc, "_pane_command", return_value=command), patch.object(
        sc, "_capture", return_value=pane_text
    ):
        return sc.session_context("s", warn_threshold=20)


def test_agent_session_healthy():
    c = _ctx("node", "\n".join([META_LINE, WIDE_BAR]))
    assert c.is_agent is True
    assert c.remaining_pct == 92
    assert c.model == "opus"
    assert c.flagged is False


def test_agent_session_flagged_when_low():
    c = _ctx("2.1.185", "\n".join([META_LINE, LOW_BAR]))
    assert c.is_agent is True
    assert c.remaining_pct == 12
    assert c.flagged is True  # 12 <= 20 warn threshold
    assert "LOW" in c.note


def test_daemon_skipped_gracefully():
    c = _ctx("python3.13", "INFO: GET /health 200 OK")
    assert c.is_agent is False
    assert c.remaining_pct is None
    assert c.flagged is False
    assert "daemon" in c.note


def test_agent_pane_no_bar_visible():
    c = _ctx("node", "...busy render, no footer yet...")
    assert c.is_agent is True
    assert c.remaining_pct is None
    assert c.flagged is False
    assert "no bar" in c.note


def test_threshold_boundary_inclusive():
    at = _ctx("node", "\n".join([META_LINE, "[██░░] 20%"]))
    assert at.flagged is True  # remaining == threshold is flagged
    above = _ctx("node", "\n".join([META_LINE, "[██░░] 21%"]))
    assert above.flagged is False
