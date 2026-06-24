"""Tests for agentwire/wiki.py — the deterministic wiki mechanical-ops module.

Stdlib only, no build needed. Every test runs against a temp wiki fixture so the
live wiki at ~/.agentwire/wiki/ is never touched.
"""

from datetime import date
from pathlib import Path

import pytest

from agentwire import wiki

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _write(root: Path, rel: str, body: str) -> Path:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    """A small, well-formed wiki with cross-links."""
    _write(tmp_path, "technologies/xterm.md",
           "---\nname: xterm.js\ncategory: terminal\nstatus: in-use\nlast_updated: 2026-06-01\n---\n\n"
           "# xterm.js\n\nWe render terminals with it. See [[winbox]] for windowing.\n")
    _write(tmp_path, "technologies/winbox.md",
           "---\nname: WinBox\ncategory: ui\nstatus: in-use\nlast_updated: 2026-06-02\n---\n\n"
           "# WinBox\n\nFloating windows. Pairs with [[xterm]].\n")
    return tmp_path


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #

def test_path_resolution(tmp_path):
    assert wiki.pages_dir(tmp_path) == tmp_path / "wiki"
    assert wiki.raw_dir(tmp_path) == tmp_path / "raw"
    assert wiki.processed_dir(tmp_path) == tmp_path / "raw" / "processed"


def test_default_root_is_home():
    assert wiki.DEFAULT_WIKI_ROOT == Path.home() / ".agentwire" / "wiki"


# --------------------------------------------------------------------------- #
# Frontmatter parsing
# --------------------------------------------------------------------------- #

def test_parse_frontmatter_basic():
    fm, body_line = wiki.parse_frontmatter("---\nname: Foo\nlast_updated: 2026-01-01\n---\n\n# Foo\n")
    assert fm == {"name": "Foo", "last_updated": "2026-01-01"}
    assert body_line == 5


def test_parse_frontmatter_missing():
    fm, body_line = wiki.parse_frontmatter("# No frontmatter\n\nbody")
    assert fm == {}
    assert body_line == 1


def test_parse_frontmatter_unterminated():
    fm, _ = wiki.parse_frontmatter("---\nname: Foo\nno closing fence\n")
    assert fm == {}


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def test_status_counts(wiki_root):
    data = wiki.status(wiki_root)
    assert data["pages"] == 2
    assert data["by_category"] == {"technologies": 2}
    assert data["unprocessed_raw"] == []


def test_status_unprocessed_raw_excludes_processed(tmp_path):
    raw = tmp_path / "raw"
    (raw / "processed").mkdir(parents=True)
    (raw / "fresh.md").write_text("x")
    (raw / "processed" / "old.md").write_text("x")
    (raw / ".hidden").write_text("x")
    data = wiki.status(tmp_path)
    assert data["unprocessed_raw"] == ["fresh.md"]
    assert data["unprocessed_raw_count"] == 1


# --------------------------------------------------------------------------- #
# query ranking
# --------------------------------------------------------------------------- #

def test_query_ranks_name_over_body(tmp_path):
    # Isolate the *name* weight: the winner has the term ONLY in frontmatter
    # `name:` — not in its stem (file is q1.md) and not in any heading — while
    # the loser has it many times in the body. So the test fails if _W_NAME is
    # neutralized, unconfounded by stem/heading weight.
    _write(tmp_path, "patterns/q1.md",
           "---\nname: kryptonite\nlast_updated: 2026-06-01\n---\n\n# Page One\n\nordinary prose\n")
    _write(tmp_path, "patterns/q2.md",
           "---\nname: Two\nlast_updated: 2026-06-01\n---\n\n# Page Two\n\nkryptonite kryptonite kryptonite kryptonite\n")
    results = wiki.query("kryptonite", tmp_path)
    assert results[0]["rel"] == "patterns/q1.md"  # single name hit (×10) beats 4 body hits (×1)


def test_query_empty_returns_nothing(wiki_root):
    assert wiki.query("", wiki_root) == []
    assert wiki.query("nonexistentterm", wiki_root) == []


def test_query_limit(tmp_path):
    for i in range(5):
        _write(tmp_path, f"patterns/p{i}.md",
               f"---\nname: p{i}\nlast_updated: 2026-06-01\n---\n\nshared shared\n")
    results = wiki.query("shared", tmp_path, limit=2)
    assert len(results) == 2


def test_query_snippet_prefers_matching_line(tmp_path):
    _write(tmp_path, "patterns/x.md",
           "---\nname: X\nlast_updated: 2026-06-01\n---\n\n# X\n\nintro line\nthe special keyword here\n")
    results = wiki.query("keyword", tmp_path)
    assert "special keyword" in results[0]["snippet"]


# --------------------------------------------------------------------------- #
# structural lint detectors
# --------------------------------------------------------------------------- #

def _kinds(findings, kind):
    return [f for f in findings if f.kind == kind]


def test_lint_stale(tmp_path):
    _write(tmp_path, "patterns/old.md",
           "---\nname: Old\nlast_updated: 2026-01-01\n---\n\nbody\n")
    findings = wiki.structural_lint(tmp_path, today=date(2026, 6, 24))
    stale = _kinds(findings, "stale")
    assert len(stale) == 1
    assert stale[0].wiki_file == "patterns/old.md"


def test_lint_not_stale_within_90d(tmp_path):
    _write(tmp_path, "patterns/recent.md",
           "---\nname: Recent\nlast_updated: 2026-06-01\n---\n\nbody\n")
    findings = wiki.structural_lint(tmp_path, today=date(2026, 6, 24))
    assert _kinds(findings, "stale") == []


def test_lint_orphan(wiki_root):
    # Add a third page nobody links to.
    _write(wiki_root, "patterns/lonely.md",
           "---\nname: Lonely\nlast_updated: 2026-06-01\n---\n\nno inbound links\n")
    findings = wiki.structural_lint(wiki_root, today=date(2026, 6, 24))
    orphans = {f.wiki_file for f in _kinds(findings, "orphan")}
    assert "patterns/lonely.md" in orphans
    # xterm and winbox link to each other → not orphans.
    assert "technologies/xterm.md" not in orphans
    assert "technologies/winbox.md" not in orphans


def test_lint_broken_link(tmp_path):
    _write(tmp_path, "patterns/a.md",
           "---\nname: A\nlast_updated: 2026-06-01\n---\n\nlinks to [[ghost]] which is gone\n")
    findings = wiki.structural_lint(tmp_path, today=date(2026, 6, 24))
    broken = _kinds(findings, "broken-link")
    assert len(broken) == 1
    assert "ghost" in broken[0].reason
    assert broken[0].line == 6


def test_lint_broken_link_resolves_alias_and_anchor(tmp_path):
    _write(tmp_path, "patterns/a.md",
           "---\nname: A\nlast_updated: 2026-06-01\n---\n\nsee [[b#section|nice alias]]\n")
    _write(tmp_path, "patterns/b.md",
           "---\nname: B\nlast_updated: 2026-06-01\n---\n\nlinked from [[a]]\n")
    findings = wiki.structural_lint(tmp_path, today=date(2026, 6, 24))
    assert _kinds(findings, "broken-link") == []


def test_lint_broken_link_resolves_bare_alias(tmp_path):
    # Alias with NO anchor — pins the alias-strip independently of anchor-strip.
    _write(tmp_path, "patterns/a.md",
           "---\nname: A\nlast_updated: 2026-06-01\n---\n\nsee [[b|just an alias]]\n")
    _write(tmp_path, "patterns/b.md",
           "---\nname: B\nlast_updated: 2026-06-01\n---\n\nlinked from [[a]]\n")
    findings = wiki.structural_lint(tmp_path, today=date(2026, 6, 24))
    assert _kinds(findings, "broken-link") == []


def test_lint_missing_frontmatter(tmp_path):
    _write(tmp_path, "patterns/bare.md", "# Bare\n\nno frontmatter at all\n")
    findings = wiki.structural_lint(tmp_path, today=date(2026, 6, 24))
    fm = _kinds(findings, "frontmatter")
    assert any("missing or unterminated" in f.reason for f in fm)


def test_lint_invalid_date(tmp_path):
    _write(tmp_path, "patterns/bad.md",
           "---\nname: Bad\nlast_updated: yesterday\n---\n\n[[bad]] self\n")
    findings = wiki.structural_lint(tmp_path, today=date(2026, 6, 24))
    fm = _kinds(findings, "frontmatter")
    assert any("not a YYYY-MM-DD" in f.reason for f in fm)


def test_lint_missing_name(tmp_path):
    _write(tmp_path, "patterns/noname.md",
           "---\ncategory: ui\nlast_updated: 2026-06-01\n---\n\nbody\n")
    findings = wiki.structural_lint(tmp_path, today=date(2026, 6, 24))
    assert any("missing 'name'" in f.reason for f in _kinds(findings, "frontmatter"))


def test_lint_returns_dicts(wiki_root):
    findings = wiki.lint(wiki_root, today=date(2026, 6, 24))
    assert all(isinstance(f, dict) for f in findings)
    assert all({"wiki_file", "line", "kind", "claim", "reason"} <= set(f) for f in findings)


def test_lint_folds_in_ground_truth(tmp_path):
    # The core "reuse wiki_audit" deliverable: lint() MUST surface ground-truth
    # findings, not just structural ones. The page below makes two checkable
    # claims that wiki_audit flags against the real repo — a nonexistent repo
    # path and a nonexistent agentwire subcommand. If the wiki_audit.audit()
    # fold-in is dropped from lint(), neither appears and this test fails.
    _write(tmp_path, "technologies/claims.md",
           "---\nname: Claims\ncategory: infra\nstatus: in-use\nlast_updated: 2026-06-01\n---\n\n"
           "# Claims\n\nSelf-link [[claims]] to avoid an orphan finding.\n\n"
           "See `agentwire/does_not_exist_xyz.py` and run `agentwire bogus-subcmd-xyz`.\n")
    findings = wiki.lint(tmp_path, today=date(2026, 6, 24))
    kinds = {f["kind"] for f in findings}
    # Ground-truth kinds — produced only by wiki_audit, never by structural_lint.
    assert "path" in kinds or "subcommand" in kinds
    paths = {f["claim"] for f in findings if f["kind"] == "path"}
    assert "agentwire/does_not_exist_xyz.py" in paths


# --------------------------------------------------------------------------- #
# new scaffold
# --------------------------------------------------------------------------- #

def test_new_page_scaffold(tmp_path):
    dest = wiki.new_page("technologies", "Redis Cache", tmp_path, today=date(2026, 6, 24))
    assert dest == tmp_path / "wiki" / "technologies" / "redis-cache.md"
    text = dest.read_text()
    assert "name: Redis Cache" in text
    assert "category:" in text  # technology-specific field
    assert "last_updated: 2026-06-24" in text
    assert "# Redis Cache" in text


def test_new_page_with_title(tmp_path):
    dest = wiki.new_page("patterns", "foo", tmp_path, title="The Foo Pattern", today=date(2026, 6, 24))
    text = dest.read_text()
    assert "name: The Foo Pattern" in text
    assert "context:" in text


def test_new_page_refuses_existing(tmp_path):
    wiki.new_page("research", "topic", tmp_path)
    with pytest.raises(FileExistsError):
        wiki.new_page("research", "topic", tmp_path)


def test_new_page_rejects_bad_category(tmp_path):
    with pytest.raises(ValueError):
        wiki.new_page("bogus", "x", tmp_path)


def test_new_page_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        wiki.new_page("patterns", "../escape", tmp_path)


# --------------------------------------------------------------------------- #
# done (raw archive move)
# --------------------------------------------------------------------------- #

def test_done_moves_to_processed(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    src = raw / "source.md"
    src.write_text("verbatim findings")
    dest = wiki.done("source.md", tmp_path)
    assert dest == raw / "processed" / "source.md"
    assert dest.read_text() == "verbatim findings"
    assert not src.exists()


def test_done_refuses_missing(tmp_path):
    (tmp_path / "raw").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        wiki.done("nope.md", tmp_path)


def test_done_refuses_nested_path(tmp_path):
    raw = tmp_path / "raw"
    (raw / "processed").mkdir(parents=True)
    (raw / "processed" / "already.md").write_text("x")
    with pytest.raises(ValueError):
        wiki.done("processed/already.md", tmp_path)
