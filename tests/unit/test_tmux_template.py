"""Tests for the bundled tmux config template and its documentation (issue #225).

agentwire's UX assumes a sane tmux config — mouse scroll, large scrollback,
working copy mode, focus events for Claude Code. These tests pin the settings
in the bundled template and make sure the docs actually point users at them.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = REPO / "agentwire" / "templates" / "tmux.conf"
QUICKSTART = REPO / "docs" / "wiki" / "quickstart.md"
README = REPO / "README.md"


class TestBundledTemplate:
    """The settings that make or break the agent UX must stay in the template."""

    @pytest.mark.parametrize("setting", [
        "set -g focus-events on",        # silences Claude Code's per-session tip
        "set -g mouse on",               # scroll through agent output
        "set -g history-limit 50000",    # agent transcripts outgrow the 2000 default
        "set -s escape-time 0",
        'set -g default-terminal "screen-256color"',
        'set -ga terminal-overrides ",xterm-256color:Tc"',
        # The copy stack: vi mode, vim-style select/yank, selection survives drag end
        "setw -g mode-keys vi",
        "bind -T copy-mode-vi v send -X begin-selection",
        "bind -T copy-mode-vi y send -X copy-selection-and-cancel",
        "unbind -T copy-mode-vi MouseDragEnd1Pane",
        # Default copy-mode table also unbound — a stray drag can land there and
        # wedge the portal's chunked WebSocket-terminal paste (#471)
        "unbind -T copy-mode    MouseDragEnd1Pane",
        # Selection-aware wheel: grow selection when present, view-scroll when not (#472)
        "bind -T copy-mode-vi WheelUpPane   if -F '#{selection_present}' 'send -X -N 3 cursor-up'   'send -X -N 3 scroll-up'",
        "bind -T copy-mode-vi WheelDownPane if -F '#{selection_present}' 'send -X -N 3 cursor-down' 'send -X -N 3 scroll-down'",
        # Multi-client sizing: portal Monitor must not shrink windows, and
        # explicit resize-window calls must not lock manual mode permanently
        "set -g window-size largest",
        "set-hook -g client-attached",
        "set-hook -g after-new-session",
    ])
    def test_required_setting_present(self, setting):
        assert setting in TEMPLATE.read_text(), f"template missing: {setting}"

    def test_pane_base_index_stays_zero(self):
        # Pane 0 = orchestrator is a convention agentwire hooks rely on.
        assert "setw -g pane-base-index 0" in TEMPLATE.read_text()


class TestDocs:
    """quickstart must document the config; README must link to it."""

    def test_quickstart_has_tmux_section(self):
        text = QUICKSTART.read_text()
        assert "### Recommended tmux config" in text
        for setting in ("focus-events on", "mouse on", "history-limit 50000",
                        "MouseDragEnd1Pane", "agentwire init"):
            assert setting in text, f"quickstart tmux section missing: {setting}"

    def test_readme_links_quickstart_tmux_section(self):
        assert "quickstart.md#recommended-tmux-config" in README.read_text()


class TestOnboardingTemplatePath:
    """The onboarding flow's template path must resolve to the shipped file."""

    def test_template_exists_at_onboarding_path(self):
        import agentwire.onboarding as onboarding
        bundled = Path(onboarding.__file__).parent / "templates" / "tmux.conf"
        assert bundled.exists()
