"""Artifact-toast titles must never render as markdown links (#821 review).

The synthesized artifact-notice text embeds a caller-supplied title
(``**{title}** is ready — click to open``), and the toast frontend renders
text through a markdown subset whose link rule would turn a title like
``[Click Here](http://evil.example)`` into a real clickable anchor inside a
trusted-looking system toast. Artifact toasts therefore render with links
disabled (notifications-panel.js passes ``links: !artifact``).

These tests drive the REAL frontend renderer — static/js/utils/rich-text.js
is pure string-in/string-out precisely so this is testable under plain node,
no browser harness needed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

RICH_TEXT_JS = (
    Path(__file__).resolve().parents[2] / "agentwire" / "static" / "js" / "utils" / "rich-text.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def _render(text: str, links: bool) -> str:
    script = (
        f"import({json.dumps(RICH_TEXT_JS.as_uri())}).then(m => "
        f"process.stdout.write(JSON.stringify(m.renderRichText("
        f"{json.dumps(text)}, {{links: {json.dumps(links)}}}))))"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


class TestArtifactToastTitleNotLinkified:
    TITLE = "[Click Here](http://evil.example)"
    SYNTHESIZED = f"**{TITLE}** is ready — click to open"

    def test_links_off_renders_link_syntax_as_literal_text(self):
        html = _render(self.SYNTHESIZED, links=False)
        assert "<a " not in html
        assert self.TITLE in html  # literal, visible verbatim
        assert html.startswith("<strong>")  # bold still applies

    def test_normal_toasts_keep_links(self):
        html = _render("see [docs](https://example.com/docs)", links=True)
        assert '<a href="https://example.com/docs"' in html
        assert 'rel="noopener noreferrer"' in html

    def test_source_html_never_survives_either_mode(self):
        for links in (True, False):
            html = _render('<script>alert(1)</script>"onmouseover="x', links=links)
            assert "<script>" not in html
            assert '"onmouseover' not in html  # quotes escaped too
