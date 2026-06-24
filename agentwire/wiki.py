"""Deterministic mechanical operations for the LLM wiki.

The wiki at ``~/.agentwire/wiki/`` (Karpathy LLM-wiki pattern) is *authored
in-context* — the research session that learns something writes the page itself,
with full context, for free (see ``agentwire/roles/agentwire.md``). What that
loop was missing is a thin, deterministic surface for the *mechanical* parts:
searching, health-checking, scaffolding, and archiving sources. This module is
that surface — the single source of truth behind the ``agentwire wiki`` CLI and
the ``wiki_query`` / ``wiki_lint`` / ``wiki_status`` MCP tools.

Design notes:
- **Stdlib only.** Like ``agentwire/wiki_audit.py``, this runs as
  ``python -m agentwire.wiki`` straight from a source checkout, no build needed,
  and never depends on PyYAML (frontmatter is parsed with a tiny scanner).
- **Deterministic, zero LLM.** ``query`` ranks and returns paths + snippets; the
  *caller* synthesizes in its own context. Nothing here calls a model.
- **Never auto-fixes.** ``lint`` reports ``file:line [kind] claim → reason`` and
  folds in ``wiki_audit.audit()``'s ground-truth pass; the human decides.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

DEFAULT_WIKI_ROOT = Path.home() / ".agentwire" / "wiki"

# Page categories and the frontmatter each one scaffolds with. ``name`` and
# ``last_updated`` are required on every page; the rest mirror the schema in
# ``~/.agentwire/wiki/CLAUDE.md``.
CATEGORIES = ("technologies", "patterns", "apis", "research")
_SCHEMA_EXTRA = {
    "technologies": [("category", "<tts|stt|terminal|ui|api|database|infra>"),
                     ("status", "<in-use|evaluated|rejected|watching>")],
    "patterns": [("context", "<where this applies>")],
    "apis": [("base_url", "<endpoint>"), ("auth", "<auth method>")],
    "research": [("status", "<active|concluded|monitoring>")],
}

STALE_DAYS = 90

_RE_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


@dataclass
class LintFinding:
    """One structural problem with a wiki page (same shape as wiki_audit.Finding)."""

    wiki_file: str  # path relative to the pages dir
    line: int  # 1-based (0 when not line-specific)
    kind: str  # stale | orphan | broken-link | frontmatter
    claim: str  # the offending token / page
    reason: str  # why it was flagged
    text: str  # the source line (stripped), or ""


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #

def resolve_root(root: Path | str | None = None) -> Path:
    return Path(root).expanduser() if root else DEFAULT_WIKI_ROOT


def pages_dir(root: Path | str | None = None) -> Path:
    return resolve_root(root) / "wiki"


def raw_dir(root: Path | str | None = None) -> Path:
    return resolve_root(root) / "raw"


def processed_dir(root: Path | str | None = None) -> Path:
    return raw_dir(root) / "processed"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_frontmatter(text: str) -> tuple[dict, int]:
    """Parse a leading ``---`` YAML-ish frontmatter block.

    Returns ``(fields, body_start_line)``. ``fields`` is empty when the document
    has no frontmatter. Only flat ``key: value`` scalars are parsed (the wiki
    schema is flat) — stdlib only, so no PyYAML dependency.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 1
    fields: dict[str, str] = {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return fields, i + 2  # 1-based line after the closing fence
        m = re.match(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$", lines[i])
        if m:
            fields[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return {}, 1  # no closing fence → treat as no frontmatter


@dataclass
class Page:
    path: Path
    rel: str  # relative to pages dir, e.g. "technologies/pi.md"
    category: str
    stem: str  # filename without .md
    frontmatter: dict
    body: str
    text: str

    @property
    def name(self) -> str:
        return self.frontmatter.get("name") or self.frontmatter.get("title") or self.stem


def load_pages(root: Path | str | None = None) -> list[Page]:
    pdir = pages_dir(root)
    pages: list[Page] = []
    if not pdir.is_dir():
        return pages
    for md in sorted(pdir.rglob("*.md")):
        rel = md.relative_to(pdir)
        text = _read(md)
        fm, body_line = parse_frontmatter(text)
        body = "\n".join(text.splitlines()[body_line - 1:])
        category = rel.parts[0] if len(rel.parts) > 1 else ""
        pages.append(Page(
            path=md, rel=str(rel), category=category, stem=md.stem,
            frontmatter=fm, body=body, text=text,
        ))
    return pages


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def unprocessed_raw(root: Path | str | None = None) -> list[str]:
    """Files directly in ``raw/`` (excluding ``raw/processed/`` and dotfiles)."""
    rdir = raw_dir(root)
    if not rdir.is_dir():
        return []
    return sorted(
        p.name for p in rdir.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )


def status(root: Path | str | None = None, *, today: date | None = None) -> dict:
    pages = load_pages(root)
    by_category: dict[str, int] = {}
    for p in pages:
        by_category[p.category or "(root)"] = by_category.get(p.category or "(root)", 0) + 1
    findings = structural_lint(root, today=today)
    counts = {"stale": 0, "orphan": 0, "broken-link": 0, "frontmatter": 0}
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    raw = unprocessed_raw(root)
    return {
        "pages": len(pages),
        "by_category": dict(sorted(by_category.items())),
        "unprocessed_raw": raw,
        "unprocessed_raw_count": len(raw),
        "stale": counts["stale"],
        "orphan": counts["orphan"],
        "broken_links": counts["broken-link"],
        "frontmatter_issues": counts["frontmatter"],
    }


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #

_RE_TOKEN = re.compile(r"[a-z0-9]+")

# Weights: a hit in the page name/title is worth far more than a body hit.
_W_NAME = 10
_W_HEADING = 4
_W_BODY = 1


def _tokenize(s: str) -> list[str]:
    return _RE_TOKEN.findall(s.lower())


def query(q: str, root: Path | str | None = None, limit: int = 10) -> list[dict]:
    """Rank pages against a query. Name/title hits outweigh body hits.

    Returns ``[{path, score, snippet}]`` sorted by score desc. Deterministic and
    LLM-free — the caller reads the pages and synthesizes.
    """
    terms = _tokenize(q)
    if not terms:
        return []
    results: list[dict] = []
    for page in load_pages(root):
        name_tokens = _tokenize(page.name + " " + page.stem)
        heading_tokens = _tokenize(" ".join(
            ln for ln in page.body.splitlines() if ln.lstrip().startswith("#")
        ))
        body_tokens = _tokenize(page.body)
        score = 0
        for t in terms:
            score += _W_NAME * name_tokens.count(t)
            score += _W_HEADING * heading_tokens.count(t)
            score += _W_BODY * body_tokens.count(t)
        if score > 0:
            results.append({
                "path": str(page.path),
                "rel": page.rel,
                "score": score,
                "snippet": _snippet(page.body, terms),
            })
    results.sort(key=lambda r: (-r["score"], r["rel"]))
    return results[:limit]


def _snippet(body: str, terms: list[str], width: int = 160) -> str:
    """First body line containing any term, trimmed; else the first prose line."""
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if any(t in low for t in terms):
            return line[:width]
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            return line[:width]
    return ""


# --------------------------------------------------------------------------- #
# lint
# --------------------------------------------------------------------------- #

def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _link_target(raw: str) -> str:
    """Normalise a ``[[link]]`` body to its target stem (drop alias + anchor)."""
    target = raw.split("|", 1)[0]  # [[target|alias]]
    target = target.split("#", 1)[0]  # [[target#section]]
    target = target.strip()
    if "/" in target:
        target = target.rsplit("/", 1)[-1]
    return target.lower()


def structural_lint(root: Path | str | None = None, *, today: date | None = None) -> list[LintFinding]:
    """Stale / orphan / broken-link / frontmatter checks, implemented as code."""
    pages = load_pages(root)
    today = today or date.today()
    findings: list[LintFinding] = []

    stems = {p.stem.lower() for p in pages}
    inbound: dict[str, int] = {p.stem.lower(): 0 for p in pages}

    for page in pages:
        # Inbound links from this page to others.
        for m in _RE_WIKILINK.finditer(page.text):
            target = _link_target(m.group(1))
            if target in inbound and target != page.stem.lower():
                inbound[target] += 1

    for page in pages:
        # --- frontmatter
        fm = page.frontmatter
        if not fm:
            findings.append(LintFinding(page.rel, 1, "frontmatter", page.rel,
                                        "missing or unterminated frontmatter block", ""))
        else:
            if not (fm.get("name") or fm.get("title")):
                findings.append(LintFinding(page.rel, 1, "frontmatter", page.rel,
                                            "frontmatter missing 'name' (or 'title')", ""))
            lu = fm.get("last_updated")
            if not lu:
                findings.append(LintFinding(page.rel, 1, "frontmatter", page.rel,
                                            "frontmatter missing 'last_updated'", ""))
            elif _parse_date(lu) is None:
                findings.append(LintFinding(page.rel, 1, "frontmatter", "last_updated: " + lu,
                                            "last_updated is not a YYYY-MM-DD date", ""))

        # --- stale (only when we have a parseable date)
        lu = fm.get("last_updated") if fm else None
        d = _parse_date(lu) if lu else None
        if d is not None and (today - d).days > STALE_DAYS:
            findings.append(LintFinding(page.rel, 1, "stale", page.rel,
                                        f"last_updated {d.isoformat()} is >{STALE_DAYS} days old", ""))

        # --- broken wikilinks (line-anchored)
        for lineno, line in enumerate(page.text.splitlines(), start=1):
            for m in _RE_WIKILINK.finditer(line):
                target = _link_target(m.group(1))
                if target and target not in stems:
                    findings.append(LintFinding(page.rel, lineno, "broken-link",
                                                f"[[{m.group(1)}]]",
                                                f"no page '{target}' exists", line.strip()))

    # --- orphans (no inbound links from any other page)
    for page in pages:
        if inbound.get(page.stem.lower(), 0) == 0:
            findings.append(LintFinding(page.rel, 0, "orphan", page.rel,
                                        "no other page links to it via [[wikilink]]", ""))

    findings.sort(key=lambda f: (f.wiki_file, f.line, f.kind))
    return findings


def _default_repo_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def lint(root: Path | str | None = None, *, repo_dir: Path | None = None,
         today: date | None = None) -> list[dict]:
    """Unified structural + ground-truth lint. Returns a flat list of finding dicts.

    Structural checks are implemented here; the ground-truth pass is delegated to
    ``wiki_audit.audit()`` (reused, not reimplemented).
    """
    from . import wiki_audit

    findings = [asdict(f) for f in structural_lint(root, today=today)]
    pdir = pages_dir(root)
    if pdir.is_dir():
        for f in wiki_audit.audit(pdir, repo_dir or _default_repo_dir()):
            findings.append(asdict(f))
    return findings


# --------------------------------------------------------------------------- #
# new
# --------------------------------------------------------------------------- #

def scaffold_frontmatter(category: str, name: str, title: str | None = None,
                         *, today: date | None = None) -> str:
    today = today or date.today()
    lines = ["---", f"name: {title or name}"]
    for key, placeholder in _SCHEMA_EXTRA.get(category, []):
        lines.append(f"{key}: {placeholder}")
    lines.append(f"last_updated: {today.isoformat()}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title or name}")
    lines.append("")
    return "\n".join(lines)


def new_page(category: str, name: str, root: Path | str | None = None,
             title: str | None = None, *, today: date | None = None) -> Path:
    """Scaffold ``wiki/<category>/<name>.md``. Raises if it exists."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category '{category}' (choose from {', '.join(CATEGORIES)})")
    safe = name.strip().lower().replace(" ", "-")
    if not safe or "/" in name or ".." in name:
        raise ValueError(f"invalid page name '{name}'")
    dest = pages_dir(root) / category / f"{safe}.md"
    if dest.exists():
        raise FileExistsError(f"page already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(scaffold_frontmatter(category, name.strip(), title, today=today), encoding="utf-8")
    return dest


# --------------------------------------------------------------------------- #
# done
# --------------------------------------------------------------------------- #

def done(rawfile: str, root: Path | str | None = None) -> Path:
    """Archive a consumed source: ``raw/<f>`` → ``raw/processed/<f>``.

    Refuses anything not sitting *directly* in ``raw/`` (so already-archived or
    nested files can't be moved). Content is never edited — only relocated.
    """
    name = Path(rawfile).name
    if name != rawfile.strip("/") or not name or name.startswith("."):
        raise ValueError(f"'{rawfile}' must be a plain filename directly in raw/")
    src = raw_dir(root) / name
    if not src.is_file():
        raise FileNotFoundError(f"no such file directly in raw/: {name}")
    dest_dir = processed_dir(root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    src.replace(dest)
    return dest


# --------------------------------------------------------------------------- #
# CLI (python -m agentwire.wiki) — convenience; the blessed surface is
# `agentwire wiki ...`, which dispatches into these same functions.
# --------------------------------------------------------------------------- #

def _format_lint_text(findings: list[dict]) -> str:
    if not findings:
        return "✓ Wiki lint: no structural or ground-truth issues found."
    out = [f"Wiki lint: {len(findings)} finding(s)\n"]
    by_file: dict[str, list[dict]] = {}
    for f in findings:
        by_file.setdefault(f["wiki_file"], []).append(f)
    for wf in sorted(by_file):
        out.append(f"\n{wf}")
        for f in sorted(by_file[wf], key=lambda x: (x["line"], x["kind"])):
            loc = f"{wf}:{f['line']}" if f["line"] else wf
            out.append(f"  {loc}  [{f['kind']}] {f['claim']}")
            out.append(f"      → {f['reason']}")
    out.append("\nNothing was auto-fixed — review and update the wiki.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m agentwire.wiki",
        description="Deterministic mechanical ops for the LLM wiki.",
    )
    parser.add_argument("--root", type=Path, default=None,
                        help=f"Wiki root (default: {DEFAULT_WIKI_ROOT})")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Page counts, unprocessed raw, lint summary")

    q = sub.add_parser("query", help="Ranked deterministic search")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=10)

    li = sub.add_parser("lint", help="Structural + ground-truth checks")
    li.add_argument("--strict", action="store_true", help="Exit 1 when issues found")

    n = sub.add_parser("new", help="Scaffold a new page")
    n.add_argument("category", choices=CATEGORIES)
    n.add_argument("name")
    n.add_argument("--title", default=None)

    d = sub.add_parser("done", help="Archive a raw source to raw/processed/")
    d.add_argument("rawfile")

    args = parser.parse_args(argv)
    root = args.root

    if args.cmd == "status":
        data = status(root)
        print(json.dumps(data, indent=2) if args.json else _status_text(data))
        return 0

    if args.cmd == "query":
        results = query(args.query, root, limit=args.limit)
        if args.json:
            print(json.dumps(results, indent=2))
        elif not results:
            print("No matching pages.")
        else:
            for r in results:
                print(f"  [{r['score']:>3}] {r['rel']}\n        {r['snippet']}")
        return 0

    if args.cmd == "lint":
        findings = lint(root)
        print(json.dumps(findings, indent=2) if args.json else _format_lint_text(findings))
        return 1 if (findings and args.strict) else 0

    if args.cmd == "new":
        try:
            dest = new_page(args.category, args.name, root, title=args.title)
        except (ValueError, FileExistsError) as e:
            print(json.dumps({"success": False, "error": str(e)}) if args.json else f"Error: {e}",
                  file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"success": True, "path": str(dest)}))
        else:
            print(f"Created {dest}")
        return 0

    if args.cmd == "done":
        try:
            dest = done(args.rawfile, root)
        except (ValueError, FileNotFoundError) as e:
            print(json.dumps({"success": False, "error": str(e)}) if args.json else f"Error: {e}",
                  file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"success": True, "path": str(dest)}))
        else:
            print(f"Archived → {dest}")
        return 0

    return 0


def _status_text(data: dict) -> str:
    lines = [f"Wiki: {data['pages']} page(s)"]
    for cat, n in data["by_category"].items():
        lines.append(f"  {cat}: {n}")
    lines.append(f"Unprocessed raw/: {data['unprocessed_raw_count']}")
    for f in data["unprocessed_raw"]:
        lines.append(f"  • {f}")
    lines.append(f"Stale: {data['stale']}  Orphan: {data['orphan']}  "
                 f"Broken links: {data['broken_links']}  Frontmatter issues: {data['frontmatter_issues']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
