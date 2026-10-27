"""Tests for agentwire/wiki.py — the deterministic wiki mechanical-ops module.

Stdlib only, no build needed. Every test runs against a temp wiki fixture so the
live wiki at ~/.agentwire/wiki/ is never touched.
"""

from datetime import date
from pathlib import Path

from agentwire import wiki

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _write(root: Path, rel: str, body: str) -> Path:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p




# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #





# --------------------------------------------------------------------------- #
# Frontmatter parsing
# --------------------------------------------------------------------------- #







# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #





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








# --------------------------------------------------------------------------- #
# structural lint detectors
# --------------------------------------------------------------------------- #

def _kinds(findings, kind):
    return [f for f in findings if f.kind == kind]








def test_lint_broken_link(tmp_path):
    _write(tmp_path, "patterns/a.md",
           "---\nname: A\nlast_updated: 2026-06-01\n---\n\nlinks to [[ghost]] which is gone\n")
    findings = wiki.structural_lint(tmp_path, today=date(2026, 6, 24))
    broken = _kinds(findings, "broken-link")
    assert len(broken) == 1
    assert "ghost" in broken[0].reason
    assert broken[0].line == 6
















# --------------------------------------------------------------------------- #
# new scaffold
# --------------------------------------------------------------------------- #











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




