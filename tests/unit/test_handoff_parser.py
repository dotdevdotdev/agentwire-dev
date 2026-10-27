"""Tests for agentwire/handoff/parser.py."""


from agentwire.handoff.parser import parse


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







class TestOpaqueSections:
    """When project_state contains a git diff that itself contains XML tags
    (e.g. of this very file), the parser must NOT match those — only the
    canonical top-level sections.
    """



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


