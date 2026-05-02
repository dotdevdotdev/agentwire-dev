"""Tests for agentwire/handoff/parser.py."""

import pytest

from agentwire.handoff.parser import HandoffParseError, parse


def _minimal_valid() -> str:
    return """\
<session_bundle version="1">
<title>Test Session</title>
<metadata>
- cwd: /tmp/foo
- branch: main
- model: claude-opus-4-7
</metadata>
<instructions>
<file path="~/.claude/CLAUDE.md" kind="claude_md">
hello
</file>
</instructions>
<project_state>
<git_status>(clean)</git_status>
</project_state>
<conversation_summary>
<goal>Test the parser.</goal>
<tldr>It works.</tldr>
</conversation_summary>
<handoff>
<one_sentence>Continue the work.</one_sentence>
<resume_at>tests/</resume_at>
</handoff>
<theme>
{"name":"t","mood":"neutral","palette":{"bg":"#000","surface":"#111","fg":"#fff","muted":"#888","accent":"#0ff","accent_2":"#ff0","border":"#222"}}
</theme>
</session_bundle>
"""


class TestParseValid:
    def test_minimal_valid_parses(self):
        bundle = parse(_minimal_valid())
        assert bundle.version == "1"
        assert bundle.title == "Test Session"
        assert bundle.metadata.cwd == "/tmp/foo"
        assert bundle.metadata.branch == "main"
        assert bundle.metadata.model == "claude-opus-4-7"
        assert len(bundle.instructions) == 1
        assert bundle.instructions[0].kind == "claude_md"
        assert bundle.summary.goal == "Test the parser."
        assert bundle.handoff.one_sentence == "Continue the work."
        assert bundle.theme.name == "t"
        assert bundle.theme.palette["accent"] == "#0ff"

    def test_raw_markdown_preserved(self):
        text = _minimal_valid()
        bundle = parse(text)
        assert bundle.raw_markdown == text

    def test_decisions_with_alternatives(self):
        text = _minimal_valid().replace(
            "<tldr>It works.</tldr>",
            """<tldr>It works.</tldr>
<decisions>
<decision>
<title>Pick A</title>
<rationale>Because</rationale>
<alternatives>
- option B
- option C
</alternatives>
</decision>
</decisions>""",
        )
        bundle = parse(text)
        assert len(bundle.summary.decisions) == 1
        d = bundle.summary.decisions[0]
        assert d.title == "Pick A"
        assert d.rationale == "Because"
        assert d.alternatives_considered == ["option B", "option C"]

    def test_journey_beats(self):
        text = _minimal_valid().replace(
            "</handoff>",
            """</handoff>
<journey>
<beat title="Discovery">
<quote>I think...</quote>
<what_happened>Things changed.</what_happened>
</beat>
<beat title="Resolution">
<what_happened>Done.</what_happened>
</beat>
</journey>""",
        )
        # journey is searched in body, position is fine
        bundle = parse(text)
        assert len(bundle.journey) == 2
        assert bundle.journey[0].title == "Discovery"
        assert bundle.journey[0].quote == "I think..."
        assert bundle.journey[1].quote is None

    def test_stats_parsing(self):
        text = _minimal_valid().replace(
            "<tldr>It works.</tldr>",
            """<tldr>It works.</tldr>
<stats>
- turns: 42
- files_touched: 7
- tools_used: 100
- duration_minutes: 134
</stats>""",
        )
        bundle = parse(text)
        assert bundle.summary.stats.turns == 42
        assert bundle.summary.stats.files_touched == 7
        assert bundle.summary.stats.tools_used == 100
        assert bundle.summary.stats.duration_minutes == 134

    def test_caveats_as_bullets(self):
        text = _minimal_valid().replace(
            "<resume_at>tests/</resume_at>",
            """<resume_at>tests/</resume_at>
<caveats>
- don't push
- ask before commits
</caveats>""",
        )
        bundle = parse(text)
        assert bundle.handoff.caveats == ["don't push", "ask before commits"]


class TestParseInvalid:
    def test_missing_metadata_tag(self):
        text = _minimal_valid().replace("<metadata>", "<xxx>").replace("</metadata>", "</xxx>")
        with pytest.raises(HandoffParseError, match="metadata"):
            parse(text)

    def test_missing_theme_tag(self):
        text = _minimal_valid().split("<theme>")[0] + "</session_bundle>\n"
        with pytest.raises(HandoffParseError, match="theme"):
            parse(text)

    def test_malformed_theme_json(self):
        text = _minimal_valid().replace(
            '{"name":"t","mood":"neutral","palette":{"bg":"#000","surface":"#111","fg":"#fff","muted":"#888","accent":"#0ff","accent_2":"#ff0","border":"#222"}}',
            "{not valid json",
        )
        with pytest.raises(HandoffParseError, match="invalid JSON"):
            parse(text)

    def test_empty_instructions_block(self):
        text = _minimal_valid().replace(
            '<file path="~/.claude/CLAUDE.md" kind="claude_md">\nhello\n</file>',
            "",
        )
        with pytest.raises(HandoffParseError, match="<file"):
            parse(text)


class TestOpaqueSections:
    """When project_state contains a git diff that itself contains XML tags
    (e.g. of this very file), the parser must NOT match those — only the
    canonical top-level sections.
    """

    def test_diff_containing_theme_does_not_confuse_parser(self):
        # Simulate a git diff that includes the template's own theme block.
        polluted = _minimal_valid().replace(
            "<git_status>(clean)</git_status>",
            """<git_status>M file.py</git_status>
<git_diff>
+<theme>
+{
+  "name": "{{ placeholder }}",
+  "mood": "{{ placeholder }}",
+  "palette": {"bg": "#000"}
+}
+</theme>
+<handoff>
+<one_sentence>{{ placeholder }}</one_sentence>
+</handoff>
</git_diff>""",
        )
        bundle = parse(polluted)
        # The REAL theme should win, not the diff one.
        assert bundle.theme.name == "t"
        assert bundle.theme.palette["accent"] == "#0ff"
        # Real handoff should win, not the diff one.
        assert bundle.handoff.one_sentence == "Continue the work."

    def test_instructions_with_xml_in_content(self):
        # CLAUDE.md content might quote XML examples.
        polluted = _minimal_valid().replace(
            "hello",
            "see <theme>example</theme> for more",
        )
        bundle = parse(polluted)
        assert bundle.theme.name == "t"  # real theme, not the inlined example
        assert "<theme>example</theme>" in bundle.instructions[0].content

    def test_diff_containing_literal_closing_project_state(self):
        # Real-world hit: a git diff inside <project_state> contains the
        # template's literal "</project_state>" string. Greedy match must take
        # the canonical (last) closing tag, not the inner one.
        polluted = _minimal_valid().replace(
            "<git_status>(clean)</git_status>",
            """<git_status>M file.py</git_status>
<git_diff>
+<project_state>
+  hello
+</project_state>
+<theme>{"name":"fake"}</theme>
</git_diff>""",
        )
        bundle = parse(polluted)
        # Real theme wins (the JSON inside the fake one would parse but with name="fake")
        assert bundle.theme.name == "t"


class TestThemeFences:
    def test_theme_in_code_fence(self):
        text = _minimal_valid().replace(
            '{"name":"t","mood":"neutral","palette":{"bg":"#000","surface":"#111","fg":"#fff","muted":"#888","accent":"#0ff","accent_2":"#ff0","border":"#222"}}',
            '```json\n{"name":"fenced","mood":"calm","palette":{"bg":"#000","surface":"#111","fg":"#fff","muted":"#888","accent":"#0ff","accent_2":"#ff0","border":"#222"}}\n```',
        )
        bundle = parse(text)
        assert bundle.theme.name == "fenced"
