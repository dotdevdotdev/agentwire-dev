"""Unit tests for the wiki ground-truth audit (agentwire/wiki_audit.py).

Each test builds a tiny fake repo + wiki text in tmp_path so the extractors and
verifiers are exercised in isolation, then asserts on the precise findings. The
final test reproduces the issue's end-to-end check: rename a flag in a wiki page
and confirm the audit flags exactly that line and nothing else.
"""

from pathlib import Path

import pytest

from agentwire.wiki_audit import audit, audit_text, build_codebase_index, main


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


@pytest.fixture
def idx(repo: Path):
    return build_codebase_index(repo)


def _kinds(findings):
    return [(f.kind, f.claim) for f in findings]


# --- index construction -----------------------------------------------------

def test_index_collects_vocabulary(idx):
    assert {"portal", "new"} <= idx.subcommands
    assert {"--dev", "--json", "--help"} <= idx.flags
    assert {"stt", "backend"} <= idx.config_fields
    assert "prompt_router" in idx.modules and "server" in idx.modules


# --- flags ------------------------------------------------------------------

def test_valid_flag_not_flagged(idx):
    assert audit_text(idx, "p.md", "Run `agentwire portal start --dev`.") == []


def test_unknown_flag_flagged(idx):
    findings = audit_text(idx, "p.md", "Run `agentwire portal start --devmode`.")
    assert _kinds(findings) == [("flag", "--devmode")]


def test_flag_only_scoped_to_agentwire_command(idx):
    # A non-agentwire tool's flag in the same doc must not be attributed to agentwire.
    assert audit_text(idx, "p.md", "Pi uses `pi --provider zai` and `--disallowedTools`.") == []


def test_flag_ignored_in_bare_prose(idx):
    # Only code spans count; prose mentioning --dev is not a claim.
    assert audit_text(idx, "p.md", "the --devmode flag is gone") == []


# --- subcommands ------------------------------------------------------------

def test_valid_subcommand_not_flagged(idx):
    assert audit_text(idx, "p.md", "`agentwire new -s proj/branch`") == []


def test_removed_subcommand_flagged(idx):
    findings = audit_text(idx, "p.md", "Use `agentwire brave search`.")
    assert ("subcommand", "agentwire brave") in _kinds(findings)


def test_subcommand_ignored_in_prose(idx):
    # "agentwire" as the product name + an English word is not a command.
    assert audit_text(idx, "p.md", "The agentwire project would benefit from this.") == []


def test_version_string_not_a_subcommand(idx):
    assert audit_text(idx, "p.md", 'Render `"agentwire v1.35.1"` verbatim.') == []


# --- paths ------------------------------------------------------------------

def test_existing_path_not_flagged(idx):
    assert audit_text(idx, "p.md", "See `agentwire/server.py` and `docs/real.md`.") == []


def test_missing_path_flagged(idx):
    findings = audit_text(idx, "p.md", "See `agentwire/sdk/client.py`.")
    assert _kinds(findings) == [("path", "agentwire/sdk/client.py")]


def test_placeholder_path_skipped(idx):
    assert audit_text(idx, "p.md", "Edit `agentwire/<module>.py` or `static/js/*`.") == []


def test_non_repo_paths_ignored(idx):
    # Home/runtime paths and the wiki's own tree are not codebase claims.
    assert audit_text(idx, "p.md", "Lives at `~/.agentwire/wiki/foo.md` and `/usr/bin/pi`.") == []


# --- symbols ----------------------------------------------------------------

def test_defined_symbol_not_flagged(idx):
    assert audit_text(idx, "p.md", "`prompt_router.prompt_is_empty` guards delivery.") == []


def test_undefined_symbol_flagged(idx):
    findings = audit_text(idx, "p.md", "`prompt_router.is_blank` is checked.")
    assert _kinds(findings) == [("symbol", "prompt_router.is_blank")]


def test_common_method_call_not_flagged(idx):
    # config.get(...) is a method on a variable, not agentwire.config.get.
    assert audit_text(idx, "p.md", "We call `config.get('stt')` here.") == []


def test_filename_not_treated_as_symbol(idx):
    assert audit_text(idx, "p.md", "Edit `server.py` and `__main__.py`.") == []


# --- config keys ------------------------------------------------------------

def test_valid_config_key_not_flagged(idx):
    assert audit_text(idx, "p.md", "Set `stt.backend` in config.yaml.") == []


def test_unknown_config_key_flagged(idx):
    findings = audit_text(idx, "p.md", "Set `stt.engdrive` in config.yaml.")
    assert _kinds(findings) == [("config-key", "stt.engdrive")]


def test_config_key_needs_config_context(idx):
    # Without a config-file mention, a dotted token isn't treated as a config key.
    assert audit_text(idx, "p.md", "The value `stt.engdrive` appears.") == []


# --- end-to-end (issue verification) ----------------------------------------

def test_rename_a_flag_flags_exactly_that_line(repo: Path, tmp_path: Path):
    wiki = tmp_path / "wiki"
    (wiki / "patterns").mkdir(parents=True)
    # One stale line (renamed flag) among otherwise-correct claims.
    (wiki / "patterns" / "dev.md").write_text(
        "# Dev\n\n"
        "Normal restart: `agentwire portal restart --dev`.\n"
        "After a change: `agentwire portal start --devmode`.\n"  # stale: --dev was renamed
        "Paths like `agentwire/server.py` still resolve.\n"
    )
    findings = audit(wiki, repo)
    assert len(findings) == 1
    f = findings[0]
    assert (f.kind, f.claim, f.line) == ("flag", "--devmode", 4)
    assert f.wiki_file == "patterns/dev.md"


def test_main_strict_exit_code(repo: Path, tmp_path: Path, capsys):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "ok.md").write_text("All good: `agentwire portal start --dev`.\n")
    (wiki / "bad.md").write_text("Stale: `agentwire brave`.\n")

    assert main(["--wiki-dir", str(wiki), "--repo-dir", str(repo)]) == 0  # informational
    assert main(["--wiki-dir", str(wiki), "--repo-dir", str(repo), "--strict"]) == 1
    out = capsys.readouterr().out
    assert "agentwire brave" in out
