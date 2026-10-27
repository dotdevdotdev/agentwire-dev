"""Unit tests for the wiki ground-truth audit (agentwire/wiki_audit.py).

Each test builds a tiny fake repo + wiki text in tmp_path so the extractors and
verifiers are exercised in isolation, then asserts on the precise findings. The
final test reproduces the issue's end-to-end check: rename a flag in a wiki page
and confirm the audit flags exactly that line and nothing else.
"""

from pathlib import Path

import pytest

from agentwire.wiki_audit import main


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal agentwire-shaped repo: known subcommands, flags, config, modules."""
    pkg = tmp_path / "agentwire"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(
        'sub.add_parser("portal")\n'
        'sub.add_parser("new")\n'
        'p.add_argument("--dev")\n'
        'p.add_argument("--json")\n'
    )
    (pkg / "config.py").write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class STTConfig:\n"
        '    backend: str = "default"\n\n'
        "@dataclass\n"
        "class Config:\n"
        "    stt: STTConfig = None\n"
    )
    (pkg / "prompt_router.py").write_text("def prompt_is_empty():\n    return True\n")
    (pkg / "server.py").write_text("# real file for path-existence checks\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "real.md").write_text("real\n")
    return tmp_path






# --- index construction -----------------------------------------------------



# --- flags ------------------------------------------------------------------









# --- subcommands ------------------------------------------------------------









# --- paths ------------------------------------------------------------------









# --- symbols ----------------------------------------------------------------









# --- config keys ------------------------------------------------------------







# --- end-to-end (issue verification) ----------------------------------------



def test_main_strict_exit_code(repo: Path, tmp_path: Path, capsys):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "ok.md").write_text("All good: `agentwire portal start --dev`.\n")
    (wiki / "bad.md").write_text("Stale: `agentwire brave`.\n")

    assert main(["--wiki-dir", str(wiki), "--repo-dir", str(repo)]) == 0  # informational
    assert main(["--wiki-dir", str(wiki), "--repo-dir", str(repo), "--strict"]) == 1
    out = capsys.readouterr().out
    assert "agentwire brave" in out
