"""MCP tools — wiki domain."""

from .mcp_core import (
    mcp,
)


@mcp.tool()
def wiki_query(q: str, limit: int = 10) -> str:
    """Search the LLM wiki and return the top matching pages (deterministic, no LLM).

    Use this FIRST, before researching anything — past investigations,
    technology gotchas, debugging solutions, and API notes accumulate in the
    wiki so you don't re-research them. This ranks pages (name/title weighted
    over body) and returns paths + snippets; READ the top pages yourself and
    synthesize the answer in your own context.

    Args:
        q: Free-text query (tokenized; order-independent).
        limit: Max pages to return (default 10).

    Returns:
        Ranked ``score  path`` lines with a snippet each, or a no-match note.
    """
    from . import wiki

    results = wiki.query(q, limit=limit)
    if not results:
        return f"No wiki pages match '{q}'. Nothing recorded yet — research it, then write a page."
    lines = [f"{len(results)} match(es) for '{q}' (read the pages, then synthesize):"]
    for r in results:
        lines.append(f"- [{r['score']}] {r['path']}\n    {r['snippet']}")
    return "\n".join(lines)


@mcp.tool()
def wiki_lint(strict: bool = False) -> str:
    """Health-check the LLM wiki: structural checks + ground-truth audit (never auto-fixes).

    Structural: stale (>90d), orphan (no inbound [[link]]), broken [[links]],
    missing/invalid frontmatter. Ground-truth: verifies concrete claims
    (agentwire subcommands/flags, repo paths, config keys, Python symbols)
    against the live codebase. Report-only.

    Args:
        strict: Cosmetic here (CLI uses it as an exit code); findings are
            always returned.

    Returns:
        ``file:line [kind] claim → reason`` findings, or an all-clear.
    """
    from . import wiki

    findings = wiki.lint()
    if not findings:
        return "✓ Wiki lint: no structural or ground-truth issues found."
    lines = [f"Wiki lint: {len(findings)} finding(s) (nothing auto-fixed):"]
    for f in findings:
        loc = f"{f['wiki_file']}:{f['line']}" if f.get("line") else f["wiki_file"]
        lines.append(f"- {loc} [{f['kind']}] {f['claim']} → {f['reason']}")
    return "\n".join(lines)


@mcp.tool()
def wiki_status() -> str:
    """Summarize the LLM wiki: page counts by category, unprocessed raw/, health counts.

    Returns:
        Page totals per category, files awaiting processing directly in raw/,
        and stale/orphan/broken/frontmatter counts.
    """
    from . import wiki

    data = wiki.status()
    lines = [f"Wiki: {data['pages']} page(s)"]
    for cat, n in data["by_category"].items():
        lines.append(f"  {cat}: {n}")
    lines.append(f"Unprocessed raw/: {data['unprocessed_raw_count']}")
    for f in data["unprocessed_raw"]:
        lines.append(f"  • {f}")
    lines.append(
        f"Stale: {data['stale']}  Orphan: {data['orphan']}  "
        f"Broken links: {data['broken_links']}  Frontmatter issues: {data['frontmatter_issues']}"
    )
    return "\n".join(lines)
