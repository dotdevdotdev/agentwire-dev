"""
Parse ai-handoff.md into a BundleData.

The agent writes ai-handoff.md as XML-tagged markdown. We extract sections by
tag, parse JSON where required, and validate. Errors are loud — the agent
needs clear feedback to self-correct on a re-run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .schema import (
    REQUIRED_TAGS,
    BundleData,
    ConversationSummary,
    Decision,
    DeadEnd,
    HandoffNote,
    Instruction,
    JourneyBeat,
    Metadata,
    OpenThread,
    ProjectState,
    Stats,
    Theme,
)


class HandoffParseError(ValueError):
    """Raised when ai-handoff.md is missing required tags or has malformed JSON."""


@dataclass
class _Section:
    name: str
    content: str
    attrs: dict[str, str]


_TAG_RE_TEMPLATE = (
    r"<{tag}(?P<attrs>[^>]*)>(?P<body>.*?)</{tag}>"
)


def _extract_all(tag: str, text: str) -> list[_Section]:
    pattern = re.compile(_TAG_RE_TEMPLATE.format(tag=re.escape(tag)), re.DOTALL)
    sections: list[_Section] = []
    for match in pattern.finditer(text):
        attrs_raw = match.group("attrs") or ""
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', attrs_raw))
        sections.append(
            _Section(name=tag, content=match.group("body").strip(), attrs=attrs)
        )
    return sections


def _extract_one(tag: str, text: str, required: bool = True) -> _Section | None:
    sections = _extract_all(tag, text)
    if not sections:
        if required:
            raise HandoffParseError(f"missing required <{tag}> section")
        return None
    return sections[0]


def _kv_lines(content: str) -> dict[str, str]:
    """Parse 'key: value' lines into a dict. Tolerates leading bullets / indentation."""
    out: dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.strip().lstrip("-").strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip().lower().replace(" ", "_")] = v.strip()
    return out


def _bullet_lines(content: str) -> list[str]:
    """Extract bullet-list items as plain strings."""
    out: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith(("-", "*")):
            out.append(line.lstrip("-* ").strip())
    return out


def _parse_metadata(section: _Section) -> Metadata:
    fields = _kv_lines(section.content)
    return Metadata(
        cwd=fields.get("cwd", ""),
        repo_url=fields.get("repo_url") or fields.get("repo") or None,
        branch=fields.get("branch") or None,
        commit=fields.get("commit") or None,
        session_type=fields.get("session_type") or None,
        model=fields.get("model") or None,
        started_at=fields.get("started_at") or None,
        ended_at=fields.get("ended_at") or None,
        user_identity=fields.get("user") or fields.get("user_identity") or None,
        mcp_servers=[s.strip() for s in fields.get("mcp_servers", "").split(",") if s.strip()],
    )


def _parse_instructions(section: _Section) -> list[Instruction]:
    files = _extract_all("file", section.content)
    if not files:
        raise HandoffParseError(
            "<instructions> must contain one or more <file path=\"...\">...</file> blocks"
        )
    out: list[Instruction] = []
    for f in files:
        path = f.attrs.get("path", "<unknown>")
        kind = f.attrs.get("kind", "claude_md")
        out.append(Instruction(path=path, content=f.content, kind=kind))
    return out


def _parse_project_state(section: _Section) -> ProjectState:
    state = ProjectState()
    if status_sec := _extract_one("git_status", section.content, required=False):
        state.git_status = status_sec.content
    if diff_sec := _extract_one("git_diff", section.content, required=False):
        state.git_diff = diff_sec.content
    if log_sec := _extract_one("git_log", section.content, required=False):
        state.git_log = log_sec.content
    for kf in _extract_all("file", section.content):
        path = kf.attrs.get("path", "<unknown>")
        state.key_files[path] = kf.content
    return state


def _parse_summary(section: _Section) -> ConversationSummary:
    goal = _extract_one("goal", section.content, required=False)
    tldr = _extract_one("tldr", section.content, required=False)
    decisions_sec = _extract_one("decisions", section.content, required=False)
    dead_ends_sec = _extract_one("dead_ends", section.content, required=False)
    open_sec = _extract_one("open_threads", section.content, required=False)
    stats_sec = _extract_one("stats", section.content, required=False)

    decisions: list[Decision] = []
    if decisions_sec:
        for d in _extract_all("decision", decisions_sec.content):
            title_m = _extract_one("title", d.content, required=False)
            rationale_m = _extract_one("rationale", d.content, required=False)
            decisions.append(
                Decision(
                    title=(title_m.content if title_m else d.attrs.get("title", "")),
                    rationale=rationale_m.content if rationale_m else d.content,
                    alternatives_considered=_bullet_lines(
                        (_extract_one("alternatives", d.content, required=False) or _Section("alternatives", "", {})).content
                    ),
                )
            )

    dead_ends: list[DeadEnd] = []
    if dead_ends_sec:
        for d in _extract_all("dead_end", dead_ends_sec.content):
            title_m = _extract_one("title", d.content, required=False)
            why_m = _extract_one("why", d.content, required=False)
            dead_ends.append(
                DeadEnd(
                    title=title_m.content if title_m else d.attrs.get("title", ""),
                    why_rejected=why_m.content if why_m else d.content,
                )
            )

    open_threads: list[OpenThread] = []
    if open_sec:
        for d in _extract_all("thread", open_sec.content):
            title_m = _extract_one("title", d.content, required=False)
            note_m = _extract_one("note", d.content, required=False)
            open_threads.append(
                OpenThread(
                    title=title_m.content if title_m else d.attrs.get("title", ""),
                    note=note_m.content if note_m else d.content,
                )
            )

    stats = Stats()
    if stats_sec:
        kv = _kv_lines(stats_sec.content)
        try:
            stats.turns = int(kv.get("turns", "0"))
            stats.files_touched = int(kv.get("files_touched", "0"))
            stats.tools_used = int(kv.get("tools_used", "0"))
            if "duration_minutes" in kv:
                stats.duration_minutes = int(kv["duration_minutes"])
        except ValueError:
            pass  # leave defaults

    return ConversationSummary(
        goal=goal.content if goal else "",
        tldr=tldr.content if tldr else "",
        decisions=decisions,
        dead_ends=dead_ends,
        open_threads=open_threads,
        stats=stats,
    )


def _parse_journey(section: _Section | None) -> list[JourneyBeat]:
    if section is None:
        return []
    out: list[JourneyBeat] = []
    for b in _extract_all("beat", section.content):
        title = b.attrs.get("title", "")
        what = _extract_one("what_happened", b.content, required=False)
        quote = _extract_one("quote", b.content, required=False)
        out.append(
            JourneyBeat(
                title=title,
                what_happened=what.content if what else b.content,
                quote=quote.content if quote else None,
            )
        )
    return out


def _parse_handoff(section: _Section) -> HandoffNote:
    one = _extract_one("one_sentence", section.content, required=False)
    resume = _extract_one("resume_at", section.content, required=False)
    caveats_sec = _extract_one("caveats", section.content, required=False)
    return HandoffNote(
        one_sentence=one.content if one else "",
        resume_at=resume.content if resume else "",
        caveats=_bullet_lines(caveats_sec.content) if caveats_sec else [],
    )


def _parse_theme(section: _Section) -> Theme:
    body = section.content.strip()
    # Allow agents to wrap the JSON in a code fence.
    body = re.sub(r"^```(?:json)?\n", "", body)
    body = re.sub(r"\n```$", "", body)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise HandoffParseError(f"<theme> contains invalid JSON: {e}") from e
    theme = Theme()
    if "name" in data:
        theme.name = str(data["name"])
    if "mood" in data:
        theme.mood = str(data["mood"])
    if isinstance(data.get("palette"), dict):
        theme.palette.update({k: str(v) for k, v in data["palette"].items()})
    if isinstance(data.get("fonts"), dict):
        theme.fonts.update({k: str(v) for k, v in data["fonts"].items()})
    if "motion" in data:
        theme.motion = str(data["motion"])
    return theme


def _validate_required(text: str) -> None:
    missing = [tag for tag in REQUIRED_TAGS if f"<{tag}" not in text]
    if missing:
        raise HandoffParseError(
            f"missing required tags: {', '.join('<' + t + '>' for t in missing)}"
        )


_OPAQUE_TAGS = ("instructions", "project_state", "recent_turns")

# Line-anchored variant: requires the opening tag at column 0 of a line.
# Excludes diff lines like `+<instructions>` and indented quotes from matching.
# The closing tag is unanchored (matches the first occurrence after the
# opening). Use with re.MULTILINE | re.DOTALL.
#
# Why unanchored close: inline tags like `<title>foo</title>` have their close
# on the same line. Block tags' first close-after-opening is always the
# canonical one (since diff copies appear AFTER, never between, the canonical
# pair).
_ANCHORED_TAG_RE_TEMPLATE = r"^<{tag}(?P<attrs>[^>]*)>(?P<body>.*?)</{tag}>"


def _extract_anchored(tag: str, text: str) -> _Section | None:
    pattern = re.compile(
        _ANCHORED_TAG_RE_TEMPLATE.format(tag=re.escape(tag)),
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    attrs_raw = match.group("attrs") or ""
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', attrs_raw))
    return _Section(name=tag, content=match.group("body").strip(), attrs=attrs)


def _mask_opaque(text: str) -> tuple[str, dict[str, _Section]]:
    """Replace opaque sections (instructions/project_state/recent_turns) with
    placeholder tags before scanning for other top-level sections.

    These sections may contain raw CLAUDE.md content, git diffs, or transcript
    excerpts that quote handoff XML. Line-anchored matching restricts pairs to
    tags at column 0, so diff-prefixed copies like `+<instructions>` are
    ignored.
    """
    captured: dict[str, _Section] = {}
    masked = text
    for tag in _OPAQUE_TAGS:
        section = _extract_anchored(tag, masked)
        if section is None:
            continue
        captured[tag] = section
        pattern = re.compile(
            _ANCHORED_TAG_RE_TEMPLATE.format(tag=re.escape(tag)),
            re.DOTALL | re.MULTILINE,
        )
        masked = pattern.sub(f"<{tag}></{tag}>", masked, count=1)
    return masked, captured


def parse(text: str) -> BundleData:
    """Parse an ai-handoff.md string into a BundleData."""
    _validate_required(text)

    # Don't extract the <session_bundle> wrapper — its content may include the
    # same wrapper as a literal (e.g. in a git diff that quotes this very
    # template). Just operate on the full text; opaque-section masking handles
    # the cross-contamination.
    version_match = re.search(r'<session_bundle\b[^>]*\bversion="([^"]+)"', text)
    version = version_match.group(1) if version_match else "1"
    body = text

    masked_body, opaque = _mask_opaque(body)

    def _anchored_or_required(tag: str) -> _Section:
        sec = _extract_anchored(tag, masked_body)
        if sec is None:
            raise HandoffParseError(f"missing required <{tag}> section at column 0")
        return sec

    metadata = _parse_metadata(_anchored_or_required("metadata"))
    instructions = _parse_instructions(opaque["instructions"]) if "instructions" in opaque \
        else _parse_instructions(_extract_one("instructions", body))
    project_state = _parse_project_state(opaque["project_state"]) if "project_state" in opaque \
        else _parse_project_state(_extract_one("project_state", body))
    summary = _parse_summary(_anchored_or_required("conversation_summary"))
    journey_sec = _extract_anchored("journey", masked_body)
    journey = _parse_journey(journey_sec)
    handoff = _parse_handoff(_anchored_or_required("handoff"))
    theme = _parse_theme(_anchored_or_required("theme"))

    recent_turns = opaque["recent_turns"].content if "recent_turns" in opaque else ""

    title_sec = _extract_anchored("title", masked_body)
    title = title_sec.content if title_sec else (summary.goal or "Session Handoff")

    return BundleData(
        version=version,
        title=title,
        metadata=metadata,
        instructions=instructions,
        project_state=project_state,
        summary=summary,
        journey=journey,
        recent_turns=recent_turns,
        handoff=handoff,
        theme=theme,
        raw_markdown=text,
    )


def parse_file(path: str | Path) -> BundleData:
    return parse(Path(path).read_text(encoding="utf-8"))
