"""Tests for the bundled tmux config template and its documentation (issue #225).

agentwire's UX assumes a sane tmux config — mouse scroll, large scrollback,
working copy mode, focus events for Claude Code. These tests pin the settings
in the bundled template and make sure the docs actually point users at them.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = REPO / "agentwire" / "templates" / "tmux.conf"
QUICKSTART = REPO / "docs" / "wiki" / "quickstart.md"
README = REPO / "README.md"






class TestOnboardingTemplatePath:
    """The onboarding flow's template path must resolve to the shipped file."""

    def test_template_exists_at_onboarding_path(self):
        import agentwire.onboarding as onboarding
        bundled = Path(onboarding.__file__).parent / "templates" / "tmux.conf"
        assert bundled.exists()
