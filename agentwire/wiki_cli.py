"""CLI for the LLM wiki + Briefing Mode research dropbox.

``agentwire wiki ...`` runs the deterministic mechanical ops (status/query/
lint/new/done) over the knowledge base; ``agentwire research [dir|ensure]``
resolves (or creates) the per-session research dropbox. Authoring stays
in-context — these commands are the deterministic surface. Pure relocation
from ``__main__`` (#495).
"""

from __future__ import annotations

from pathlib import Path

from . import pane_manager
from .core import _output_json, _output_result


def cmd_research(args) -> int:
    """Resolve (or ensure) the Briefing Mode research dropbox for a session."""
    from .research import ensure_research_dir, research_dir

    json_mode = getattr(args, "json", False)
    session = getattr(args, "session", None) or pane_manager.get_current_session()
    if not session:
        return _output_result(False, json_mode, "No session (use -s or run inside a session)")
    sub = getattr(args, "research_command", None)
    path = ensure_research_dir(session) if sub == "ensure" else research_dir(session)
    if json_mode:
        _output_json({"success": True, "session": session, "path": str(path), "exists": path.exists()})
        return 0
    print(str(path))
    return 0


def cmd_wiki(args) -> int:
    """Deterministic mechanical ops for the LLM wiki (status/query/lint/new/done)."""
    from . import wiki

    json_mode = getattr(args, "json", False)
    root = getattr(args, "root", None)
    sub = getattr(args, "wiki_command", None)

    if sub is None:
        return _output_result(False, json_mode, "Specify a subcommand: status, query, lint, new, done")

    if sub == "status":
        data = wiki.status(root)
        if json_mode:
            _output_json({"success": True, **data})
            return 0
        print(wiki._status_text(data))
        return 0

    if sub == "query":
        results = wiki.query(args.query, root, limit=args.limit)
        if json_mode:
            _output_json({"success": True, "results": results})
            return 0
        if not results:
            print("No matching pages.")
            return 0
        for r in results:
            print(f"  [{r['score']:>3}] {r['rel']}\n        {r['snippet']}")
        return 0

    if sub == "lint":
        findings = wiki.lint(root)
        if json_mode:
            _output_json({"success": True, "findings": findings, "count": len(findings)})
        else:
            print(wiki._format_lint_text(findings))
        return 1 if (findings and getattr(args, "strict", False)) else 0

    if sub == "new":
        try:
            dest = wiki.new_page(args.category, args.name, root, title=getattr(args, "title", None))
        except (ValueError, FileExistsError) as e:
            return _output_result(False, json_mode, str(e))
        return _output_result(True, json_mode, f"Created {dest}", path=str(dest))

    if sub == "done":
        try:
            dest = wiki.done(args.rawfile, root)
        except (ValueError, FileNotFoundError) as e:
            return _output_result(False, json_mode, str(e))
        return _output_result(True, json_mode, f"Archived → {dest}", path=str(dest))

    return _output_result(False, json_mode, f"Unknown wiki subcommand: {sub}")


def register_wiki_parser(subparsers) -> None:
    # research: Briefing Mode dropbox path resolver
    research_parser = subparsers.add_parser("research", help="Resolve the Briefing Mode research dropbox path for a session")
    research_sub = research_parser.add_subparsers(dest="research_command")
    for _verb, _help in (("dir", "Print the dropbox path (not created)"),
                         ("ensure", "Create + print the dropbox path")):
        _rp = research_sub.add_parser(_verb, help=_help)
        _rp.add_argument("-s", "--session", default=None, help="Anchor session (default: current)")
        _rp.add_argument("--json", action="store_true", help="Output as JSON")
        _rp.set_defaults(func=cmd_research)
    research_parser.add_argument("-s", "--session", default=None, help="Anchor session (default: current)")
    research_parser.add_argument("--json", action="store_true", help="Output as JSON")
    research_parser.set_defaults(func=cmd_research)

    # wiki: deterministic mechanical ops for the LLM wiki
    from . import wiki as _wiki_mod
    wiki_parser = subparsers.add_parser("wiki", help="Deterministic mechanical ops for the LLM wiki")
    wiki_parser.add_argument("--root", type=Path, default=None,
                             help=f"Wiki root (default: {_wiki_mod.DEFAULT_WIKI_ROOT})")
    wiki_sub = wiki_parser.add_subparsers(dest="wiki_command")

    _ws = wiki_sub.add_parser("status", help="Page counts, unprocessed raw, lint summary")
    _ws.add_argument("--json", action="store_true", help="Output as JSON")
    _ws.set_defaults(func=cmd_wiki)

    _wq = wiki_sub.add_parser("query", help="Ranked deterministic search (caller synthesizes)")
    _wq.add_argument("query")
    _wq.add_argument("--limit", type=int, default=10)
    _wq.add_argument("--json", action="store_true", help="Output as JSON")
    _wq.set_defaults(func=cmd_wiki)

    _wl = wiki_sub.add_parser("lint", help="Structural + ground-truth checks (never auto-fixes)")
    _wl.add_argument("--strict", action="store_true", help="Exit 1 when issues found")
    _wl.add_argument("--json", action="store_true", help="Output as JSON")
    _wl.set_defaults(func=cmd_wiki)

    _wn = wiki_sub.add_parser("new", help="Scaffold wiki/<category>/<name>.md")
    _wn.add_argument("category", choices=_wiki_mod.CATEGORIES)
    _wn.add_argument("name")
    _wn.add_argument("--title", default=None)
    _wn.add_argument("--json", action="store_true", help="Output as JSON")
    _wn.set_defaults(func=cmd_wiki)

    _wd = wiki_sub.add_parser("done", help="Archive raw/<f> → raw/processed/<f>")
    _wd.add_argument("rawfile")
    _wd.add_argument("--json", action="store_true", help="Output as JSON")
    _wd.set_defaults(func=cmd_wiki)

    wiki_parser.set_defaults(func=cmd_wiki)
