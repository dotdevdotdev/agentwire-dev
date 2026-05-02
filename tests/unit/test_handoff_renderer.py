"""Tests for agentwire/handoff/renderer.py."""

from agentwire.handoff.parser import parse
from agentwire.handoff.renderer import render_html


_BASE = """\
<session_bundle version="1">
<title>Render Test</title>
<metadata>
- cwd: /tmp/foo
- branch: main
- model: claude-opus-4-7
- mcp_servers: agentwire, claude-in-chrome
</metadata>
<instructions>
<file path="./CLAUDE.md" kind="project_claude_md">
project rules
</file>
</instructions>
<project_state>
<git_status>M file.py</git_status>
<git_log>abc1234 hello</git_log>
<git_diff>
diff --git a/file.py b/file.py
@@ -1 +1 @@
-old
+new
</git_diff>
</project_state>
<conversation_summary>
<goal>Make sure HTML renders.</goal>
<tldr>If you can read this, it works.</tldr>
<decisions>
<decision>
<title>Use Jinja2</title>
<rationale>Already a dep.</rationale>
</decision>
</decisions>
<dead_ends>
<dead_end>
<title>Tried raw f-strings</title>
<why>Not maintainable.</why>
</dead_end>
</dead_ends>
<open_threads>
<thread>
<title>More tests</title>
<note>Coverage incomplete.</note>
</thread>
</open_threads>
</conversation_summary>
<journey>
<beat title="Setup">
<quote>Let's render this.</quote>
<what_happened>Wrote the template.</what_happened>
</beat>
</journey>
<handoff>
<one_sentence>Verify HTML opens cleanly in a browser.</one_sentence>
<resume_at>show-the-story.html</resume_at>
<caveats>
- inline only, no CDN
</caveats>
</handoff>
<theme>
{"name":"sunlit","mood":"calm","palette":{"bg":"#fafafa","surface":"#ffffff","fg":"#222","muted":"#888","accent":"#0066cc","accent_2":"#cc6600","border":"#ddd"}}
</theme>
</session_bundle>
"""


class TestRender:
    def test_render_basic(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        assert html.startswith("<!DOCTYPE html>")
        assert "<title>Render Test</title>" in html
        assert "Make sure HTML renders." in html

    def test_theme_palette_inlined(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        # Theme accent picks up in CSS variables
        assert "#0066cc" in html
        assert "#fafafa" in html

    def test_decisions_visible(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        assert "Use Jinja2" in html
        assert "Already a dep." in html

    def test_dead_ends_visible(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        assert "Tried raw f-strings" in html

    def test_journey_visible(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        assert "Setup" in html
        assert "Let's render this." in html

    def test_instructions_inlined_in_details(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        assert "./CLAUDE.md" in html
        assert "project rules" in html

    def test_raw_markdown_template_block(self):
        """The HTML embeds the raw ai-handoff.md so it's LLM-droppable too."""
        bundle = parse(_BASE)
        html = render_html(bundle)
        assert '<template id="ai-handoff">' in html
        assert "<title>Render Test</title>" in html

    def test_self_contained_no_external_assets(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        # No CDNs, external scripts, or imported stylesheets
        assert "https://cdn." not in html
        assert "<link rel=\"stylesheet\"" not in html
        assert 'src="http' not in html

    def test_tabs_present(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        for tab_label in (
            "Overview",
            "The Goal",
            "Journey",
            "Decisions",
            "Artifacts",
            "Open Threads",
            "Cast & Context",
            "Instructions",
        ):
            assert tab_label in html, f"missing tab: {tab_label}"

    def test_keyboard_nav_script_present(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        assert "ArrowRight" in html
        assert "ArrowLeft" in html


class TestThemeVariations:
    def test_dark_theme(self):
        text = _BASE.replace(
            '"bg":"#fafafa","surface":"#ffffff","fg":"#222"',
            '"bg":"#0a0e14","surface":"#11161e","fg":"#e6e1cf"',
        )
        bundle = parse(text)
        html = render_html(bundle)
        assert "#0a0e14" in html
        assert "#fafafa" not in html

    def test_motion_subtle_default(self):
        bundle = parse(_BASE)
        html = render_html(bundle)
        # subtle = 180ms transition duration
        assert "180ms" in html

    def test_motion_playful(self):
        text = _BASE.replace('"#ddd"}}', '"#ddd"},"motion":"playful"}')
        bundle = parse(text)
        html = render_html(bundle)
        assert "320ms" in html

    def test_motion_none(self):
        text = _BASE.replace('"#ddd"}}', '"#ddd"},"motion":"none"}')
        bundle = parse(text)
        html = render_html(bundle)
        assert "0ms" in html
