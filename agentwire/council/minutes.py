"""Council minutes — deterministic HTML render of a sitting's persisted record.

Handoff pattern (#708): everything verbatim (the question, the attributed
take/ack/pass replies) already persists under
``~/.agentwire/council/<name>/prompts/NNNN/``, so the renderer is a pure
function of disk state. The synthesis exists only in the orchestrator's
context, so it arrives as an *input* — omitted, the minutes render without a
synthesis section.

Output is one fully self-contained HTML file (inline CSS only — the portal's
artifact CSP blocks external fetches), theme-aware via
``prefers-color-scheme``, at::

    ~/.agentwire/artifacts/council-<name>-minutes/index.html

Works for live and dismissed sittings alike — prompt history is disk-derived,
sitting metadata falls back from ``sitting.json`` to the preserved
``archive.json``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import jinja2

from agentwire.council import inbox, state, view

# --- paths ------------------------------------------------------------------


def artifacts_dir() -> Path:
    """The portal artifacts root (config override honored)."""
    try:
        from agentwire.config import load_config

        return Path(str(load_config().artifacts.dir)).expanduser()
    except Exception:
        return Path.home() / ".agentwire" / "artifacts"


def minutes_path(name: str) -> Path:
    return artifacts_dir() / f"council-{name}-minutes" / "index.html"


def artifact_url(name: str) -> str:
    """Path relative to the artifacts root — what the portal iframe loads."""
    return f"council-{name}-minutes/index.html"


# --- gather -----------------------------------------------------------------


def _fmt_ts(iso: str) -> str:
    """ISO timestamp → compact display form (``2026-07-04 15:38 UTC``)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso
    suffix = " UTC" if dt.tzinfo is not None else ""
    return dt.strftime("%Y-%m-%d %H:%M") + suffix


def gather(name: str, prompt_ids: list[int] | None = None) -> dict | None:
    """Collect a sitting's persisted record into a render-ready dict.

    ``prompt_ids`` filters to specific rounds (None = every prompt on disk).
    Returns ``None`` when nothing matches — a sitting that never deliberated
    has no minutes. Reply dicts carry only content (soul, kind, text,
    timestamp), never local filesystem paths: the artifact is built to be
    shared.
    """
    available = view.available_prompt_ids(name)
    selected = (
        available if prompt_ids is None else [p for p in prompt_ids if p in available]
    )
    if not selected:
        return None

    sitting = state.read_sitting(name)
    archived = sitting is None
    dismissed_at = ""
    if archived:
        raw = state.read_archive_dict(name)
        dismissed_at = (raw or {}).get("dismissed_at", "")
        sitting = (
            state.Sitting.from_dict(raw)
            if raw is not None
            else state.Sitting(
                orchestrator=state.orchestrator_for(name),
                roster=[],
                sessions={},
                started_at="",
            )
        )

    prompts = []
    for pid in selected:
        meta = inbox.read_meta(name, pid)
        try:
            question = (inbox.prompt_dir(name, pid) / "prompt.md").read_text()
        except OSError:
            question = ""
        replies = [
            {
                "soul": r.soul,
                "kind": r.kind,
                "text": r.text.strip(),
                "written_at": _fmt_ts(r.written_at),
            }
            for r in inbox.list_replies(name, pid)
        ]
        prompts.append(
            {
                "id": pid,
                "question": question.strip(),
                "created_at": _fmt_ts(meta.get("created_at", "")),
                "roster": meta.get("roster") or list(sitting.roster),
                "replies": replies,
            }
        )

    roster = list(sitting.roster) or prompts[0]["roster"]
    return {
        "name": name,
        "archived": archived,
        "started_at": _fmt_ts(sitting.started_at),
        "dismissed_at": _fmt_ts(dismissed_at),
        "roster": roster,
        "prompts": prompts,
    }


# --- render -----------------------------------------------------------------


def _env() -> jinja2.Environment:
    # autoescape=True explicitly: select_autoescape keys off the *final*
    # extension, and ``.html.j2`` ends in ``.j2`` — verbatim soul takes are
    # untrusted content, so escaping must never silently switch off.
    templates = Path(__file__).resolve().parent.parent / "templates"
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates)),
        autoescape=True,
    )


def render_html(record: dict, synthesis: str = "") -> str:
    template = _env().get_template("council/minutes.html.j2")
    return template.render(
        record=record,
        synthesis=(synthesis or "").strip(),
        generated_at=_fmt_ts(state.now_iso()),
    )


def write_minutes(
    name: str, prompt_ids: list[int] | None = None, synthesis: str = ""
) -> Path | None:
    """Render and write the minutes artifact; return its path.

    Returns ``None`` (writes nothing) when the sitting has no matching prompt
    history — a zero-prompt sitting leaves no minutes behind.
    """
    record = gather(name, prompt_ids)
    if record is None:
        return None
    out = minutes_path(name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(record, synthesis), encoding="utf-8")
    return out
