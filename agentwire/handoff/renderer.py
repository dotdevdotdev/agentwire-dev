"""
Render a parsed BundleData into show-the-story.html.

We use Jinja2 directly (not aiohttp-jinja2) because this runs offline as part
of CLI/MCP, not inside the portal HTTP layer.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from .schema import BundleData


_TABS = (
    {"id": "overview", "label": "Overview"},
    {"id": "goal", "label": "The Goal"},
    {"id": "journey", "label": "Journey"},
    {"id": "decisions", "label": "Decisions"},
    {"id": "artifacts", "label": "Artifacts"},
    {"id": "open", "label": "Open Threads"},
    {"id": "cast", "label": "Cast & Context"},
    {"id": "instructions", "label": "Instructions"},
)


def _templates_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_templates_dir())),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render_html(bundle: BundleData) -> str:
    env = _env()
    template = env.get_template("handoff/show-the-story.html.j2")
    return template.render(
        bundle=bundle,
        theme=bundle.theme,
        tabs=_TABS,
    )


def render_to_file(bundle: BundleData, output_path: str | Path) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(bundle), encoding="utf-8")
    return out
