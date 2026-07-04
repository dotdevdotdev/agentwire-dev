"""Guard the #696 window-cycling keyboard contract in the portal static JS.

Owner decision on #696: Tab and Shift+Tab are NEVER intercepted by the desktop
(Claude Code needs Tab for completion and Shift+Tab for permission modes, #659);
window cycling lives on a dedicated unconditional Alt+] / Alt+[ chord instead.
There is no JS test runner in this repo, so these are source-level assertions
tying desktop.js's real binding to the shortcuts.js catalogue that feeds the
F1 help modal (the two must stay in sync — see shortcuts.js header).
"""

import re
from pathlib import Path

import pytest

STATIC_JS = Path(__file__).resolve().parents[2] / "agentwire" / "static" / "js"


@pytest.fixture(scope="module")
def desktop_js():
    return (STATIC_JS / "desktop.js").read_text()


@pytest.fixture(scope="module")
def shortcuts_js():
    return (STATIC_JS / "shortcuts.js").read_text()


@pytest.fixture(scope="module")
def cycling_handler(desktop_js):
    """The setupWindowCycling function body (up to the next top-level function)."""
    match = re.search(
        r"function setupWindowCycling\(\).*?(?=\nfunction )", desktop_js, re.DOTALL
    )
    assert match, "setupWindowCycling missing from desktop.js"
    return match.group(0)


def test_cycling_never_intercepts_tab(cycling_handler, desktop_js):
    assert "'Tab'" not in cycling_handler, (
        "setupWindowCycling must not handle Tab — Tab/Shift+Tab always pass "
        "through to the terminal (#696 owner decision)"
    )
    # The #663 focus-gate + sticky-chain machinery must stay deleted, not dormant.
    for relic in ("tabBelongsToFocusedInput", "CYCLE_GRACE_MS", "lastKeyboardCycleTs"):
        assert relic not in desktop_js, f"{relic} is #663 machinery removed by #696"


def test_cycling_bound_to_alt_brackets(cycling_handler):
    # e.code (physical key), not e.key — macOS Option composes e.key into “ / ‘.
    assert "e.code" in cycling_handler
    assert "'BracketRight'" in cycling_handler
    assert "'BracketLeft'" in cycling_handler
    assert "e.altKey" in cycling_handler
    assert "cycleWindow" in cycling_handler


def test_help_catalogue_documents_the_chord(shortcuts_js):
    row = re.search(r"\{[^{}]*setupWindowCycling[^{}]*\}", shortcuts_js)
    assert row, "shortcuts.js must keep a row pointing at setupWindowCycling"
    assert "['Alt', ']']" in row.group(0)
    assert "['Alt', '[']" in row.group(0)
    assert "['Tab']" not in row.group(0), (
        "help modal must not advertise Tab window-cycling (#696)"
    )
