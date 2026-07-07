"""CLI for handoff bundles — ``agentwire handoff ...``.

Distills a conversation into a shareable bundle: ``ai-handoff.md`` for another
LLM and ``show-the-story.html`` for humans. The agent does the distillation
in-context; this command scaffolds the bundle and renders the HTML.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

from .core import _output_json

_HANDOFF_TEMPLATE = """\
<session_bundle version="1">
<title>{{ short title — what this session is about }}</title>

<metadata>
- cwd: __CWD__
- repo_url: __REPO_URL__
- branch: __BRANCH__
- commit: __COMMIT__
- posture: bypass
- model: __MODEL__
- started_at: {{ ISO timestamp }}
- ended_at: {{ ISO timestamp }}
- user_identity: {{ who }}
- mcp_servers: {{ comma-separated list }}
</metadata>

<environment>
{{ panes, channels, scheduler state, anything else the receiver can't see from cwd alone }}
</environment>

<instructions>
{{ The CLI prefilled this section with collected CLAUDE.md / rules / memory.
   Leave it alone unless you need to redact secrets. }}
__INSTRUCTIONS_BLOCK__
</instructions>

<project_state>
<git_status>
__GIT_STATUS__
</git_status>
<git_log>
__GIT_LOG__
</git_log>
<git_diff>
__GIT_DIFF__
</git_diff>
{{ optionally <file path="..."> blocks for key files }}
</project_state>

<conversation_summary>
<goal>{{ one sentence: what this session set out to do }}</goal>
<tldr>{{ one paragraph the receiver reads first }}</tldr>
<decisions>
<decision>
<title>{{ short name of decision }}</title>
<rationale>{{ why it was made }}</rationale>
<alternatives>
- {{ alternative 1 }}
- {{ alternative 2 }}
</alternatives>
</decision>
</decisions>
<dead_ends>
<dead_end>
<title>{{ thing tried }}</title>
<why>{{ why rejected — saves the receiver from retracing }}</why>
</dead_end>
</dead_ends>
<open_threads>
<thread>
<title>{{ unresolved item }}</title>
<note>{{ where to pick up }}</note>
</thread>
</open_threads>
<stats>
- turns: {{ N }}
- files_touched: {{ N }}
- tools_used: {{ N }}
- duration_minutes: {{ N }}
</stats>
</conversation_summary>

<journey>
<beat title="{{ short name }}">
<quote>{{ optional verbatim line from the conversation }}</quote>
<what_happened>{{ what changed at this beat }}</what_happened>
</beat>
</journey>

<recent_turns>
{{ Last ~10-20 turns, filtered: drop tool noise, keep user turns + decision-making
   assistant turns. Use markdown blockquotes or simple labelled blocks. }}
</recent_turns>

<handoff>
<one_sentence>{{ what the next agent should do first }}</one_sentence>
<resume_at>{{ specific file path / TODO / step number }}</resume_at>
<caveats>
- {{ permission boundary, e.g. "do not push" }}
- {{ env-specific note }}
</caveats>
</handoff>

<theme>
{
  "name": "{{ short slug, e.g. evening-debug }}",
  "mood": "{{ honest read of the session's emotional tone }}",
  "palette": {
    "bg": "#0e0f13",
    "surface": "#1a1d24",
    "fg": "#e2e8f0",
    "muted": "#64748b",
    "accent": "#5eead4",
    "accent_2": "#fbbf24",
    "border": "#2a2f3a"
  },
  "fonts": {
    "heading": "ui-monospace, 'JetBrains Mono', monospace",
    "body": "ui-sans-serif, system-ui, sans-serif"
  },
  "motion": "subtle"
}
</theme>
</session_bundle>
"""


def _handoff_artifacts_dir() -> Path:
    try:
        from .config import load_config
        cfg = load_config()
        return Path(str(cfg.artifacts.dir)).expanduser()
    except Exception:
        return Path.home() / ".agentwire" / "artifacts"


def _handoff_slug(title_hint: str | None = None) -> str:
    import re as _re
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if title_hint:
        clean = _re.sub(r"[^a-z0-9]+", "-", title_hint.lower()).strip("-")[:40]
        if clean:
            return f"handoff-{ts}-{clean}"
    return f"handoff-{ts}"


def _build_template(cwd: Path) -> str:
    """Pre-fill template with collected git + instructions chain."""
    from .handoff import git_state, instructions

    snap = git_state.snapshot(cwd)
    chain = instructions.collect(cwd)

    instructions_block_parts = []
    for instr in chain:
        instructions_block_parts.append(
            f'<file path="{instr.path}" kind="{instr.kind}">\n{instr.content}\n</file>'
        )
    instructions_block = "\n".join(instructions_block_parts) if instructions_block_parts else \
        "{{ no CLAUDE.md or memory files found — this bundle is portable but light on instructions }}"

    substitutions = {
        "__CWD__": str(cwd),
        "__REPO_URL__": snap.get("remote_url") or "{{ none }}",
        "__BRANCH__": snap.get("branch") or "{{ unknown }}",
        "__COMMIT__": snap.get("commit") or "{{ unknown }}",
        "__MODEL__": "claude-opus-4-7",
        "__INSTRUCTIONS_BLOCK__": instructions_block,
        "__GIT_STATUS__": snap.get("status") or "(clean)",
        "__GIT_LOG__": snap.get("log") or "(no commits)",
        "__GIT_DIFF__": snap.get("diff") or "(no uncommitted diff)",
    }
    text = _HANDOFF_TEMPLATE
    for key, value in substitutions.items():
        text = text.replace(key, value)
    return text


def cmd_handoff_init(args) -> int:
    """Create a handoff bundle directory and emit a pre-filled ai-handoff.md template.

    The agent then edits ai-handoff.md to add the session-specific summary,
    decisions, journey, and theme — the parts only the agent has context for.
    """
    json_mode = getattr(args, 'json', False)
    title_hint = getattr(args, 'title', None)
    output_dir = getattr(args, 'output_dir', None)

    base = Path(output_dir).expanduser() if output_dir else _handoff_artifacts_dir()
    bundle_dir = base / _handoff_slug(title_hint)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    cwd = Path.cwd()
    template_text = _build_template(cwd)
    handoff_path = bundle_dir / "ai-handoff.md"
    handoff_path.write_text(template_text, encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "cwd": str(cwd),
        "title_hint": title_hint or "",
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    if json_mode:
        _output_json({
            "success": True,
            "bundle_dir": str(bundle_dir),
            "ai_handoff_path": str(handoff_path),
            "manifest_path": str(bundle_dir / "manifest.json"),
            "instructions": (
                "Edit ai-handoff.md to fill in the {{ ... }} placeholders. "
                "Then run: agentwire handoff render <bundle_dir> --story"
            ),
        })
    else:
        print(f"Bundle dir: {bundle_dir}")
        print(f"Edit:       {handoff_path}")
        print()
        print("Next: fill in the placeholders, then run:")
        print(f"  agentwire handoff render {bundle_dir} --story")
    return 0


def cmd_handoff_render(args) -> int:
    """Render show-the-story.html from an existing ai-handoff.md."""
    from .handoff.parser import HandoffParseError, parse_file
    from .handoff.renderer import render_to_file

    json_mode = getattr(args, 'json', False)
    target = Path(args.path).expanduser()

    if target.is_dir():
        md_path = target / "ai-handoff.md"
        bundle_dir = target
    else:
        md_path = target
        bundle_dir = target.parent

    if not md_path.exists():
        msg = f"ai-handoff.md not found at {md_path}"
        if json_mode:
            _output_json({"success": False, "error": msg})
        else:
            print(msg, file=sys.stderr)
        return 1

    try:
        bundle = parse_file(md_path)
    except HandoffParseError as e:
        msg = f"Invalid ai-handoff.md: {e}"
        if json_mode:
            _output_json({"success": False, "error": msg, "path": str(md_path)})
        else:
            print(msg, file=sys.stderr)
        return 1

    outputs: dict[str, str] = {"ai_handoff_path": str(md_path)}

    if getattr(args, 'story', True):
        story_path = bundle_dir / "show-the-story.html"
        render_to_file(bundle, story_path)
        outputs["show_the_story_path"] = str(story_path)

    if json_mode:
        _output_json({"success": True, "bundle_dir": str(bundle_dir), **outputs})
    else:
        print(f"Bundle dir: {bundle_dir}")
        for k, v in outputs.items():
            print(f"  {k}: {v}")
    return 0


def cmd_handoff_list(args) -> int:
    """List past handoff bundles in the artifacts directory."""
    json_mode = getattr(args, 'json', False)
    base = _handoff_artifacts_dir()

    bundles = []
    if base.exists():
        for d in sorted(base.glob("handoff-*")):
            if not d.is_dir():
                continue
            handoff_md = d / "ai-handoff.md"
            story_html = d / "show-the-story.html"
            manifest_path = d / "manifest.json"
            manifest_data = {}
            if manifest_path.exists():
                try:
                    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            bundles.append({
                "name": d.name,
                "path": str(d),
                "ai_handoff_exists": handoff_md.exists(),
                "show_the_story_exists": story_html.exists(),
                "created_at": manifest_data.get("created_at", ""),
                "title_hint": manifest_data.get("title_hint", ""),
                "cwd": manifest_data.get("cwd", ""),
            })

    if json_mode:
        _output_json({"success": True, "bundles": bundles, "count": len(bundles)})
    else:
        if not bundles:
            print(f"No handoff bundles in {base}")
            return 0
        print(f"Handoff bundles in {base}:")
        for b in bundles:
            badge = []
            if b["ai_handoff_exists"]:
                badge.append("md")
            if b["show_the_story_exists"]:
                badge.append("html")
            print(f"  {b['name']:<60} [{','.join(badge) or '-'}] {b['title_hint']}")
    return 0


def register_handoff_parser(subparsers) -> None:
    handoff_parser = subparsers.add_parser(
        "handoff",
        help="Shareable conversation bundles (ai-handoff.md + show-the-story.html)",
    )
    handoff_subparsers = handoff_parser.add_subparsers(dest="handoff_command")

    handoff_init_parser = handoff_subparsers.add_parser(
        "init",
        help="Create a bundle dir + pre-filled ai-handoff.md template",
    )
    handoff_init_parser.add_argument(
        "--title", help="Short title hint, used in the bundle slug"
    )
    handoff_init_parser.add_argument(
        "--output-dir", help="Override artifacts dir (default: ~/.agentwire/artifacts)"
    )
    handoff_init_parser.add_argument(
        "--json", action="store_true", help="JSON output"
    )
    handoff_init_parser.set_defaults(func=cmd_handoff_init)

    handoff_render_parser = handoff_subparsers.add_parser(
        "render",
        help="Render show-the-story.html from an existing ai-handoff.md",
    )
    handoff_render_parser.add_argument(
        "path", help="Bundle dir or path to ai-handoff.md"
    )
    handoff_render_parser.add_argument(
        "--story", action="store_true", default=True,
        help="Render show-the-story.html (default: on)",
    )
    handoff_render_parser.add_argument(
        "--no-story", dest="story", action="store_false",
        help="Skip HTML render (only validates the markdown)",
    )
    handoff_render_parser.add_argument(
        "--json", action="store_true", help="JSON output"
    )
    handoff_render_parser.set_defaults(func=cmd_handoff_render)

    handoff_list_parser = handoff_subparsers.add_parser(
        "list", help="List past handoff bundles"
    )
    handoff_list_parser.add_argument(
        "--json", action="store_true", help="JSON output"
    )
    handoff_list_parser.set_defaults(func=cmd_handoff_list)
