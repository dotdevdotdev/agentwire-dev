"""The damage-control corpus as data (#1209 M0).

``corpus/cases/*.yaml`` holds the operation-level cases that used to live as Python
lists in ``test_damage_control_bypass.py`` / ``test_control_plane_protection.py``.
One test per CLASS, not per case; every class collects its offenders and asserts none.

The legacy modules stay in M0 on purpose: they are the reference the equivalence
test below measures the corpus against. Delete a converted table and its entry in
``LEGACY_TABLES`` together once the corpus is trusted.
"""

import importlib.util
import os
from pathlib import Path

import pytest

from tests import corpus_runner as cr

REPO = cr.REPO
CASES = cr.load_cases()
CLASSES = cr.by_class(CASES)

# Every module-level table the converter lifted, as ``source`` names it. Inline lists
# (a test's own local sample) have no table and are covered by the class tests only.
LEGACY_TABLES = {
    "tests/unit/test_damage_control_bypass.py": [
        "BYPASS_VECTORS", "HARD_BLOCKS_BY_POSITION", "HARD_BLOCKS_BY_VERB", "VERB_CONCEALING",
        "OPERAND_SUBSTITUTION", "SAFE_COMMANDS", "DANGER_AS_DATA", "ZERO_ACCESS_READS",
    ],
    "tests/unit/test_control_plane_protection.py": [
        "CONTROL_PLANE_FILES", "BASH_WRITES", "INDIRECT_WRITES", "PROMOTE_COMMANDS",
        "PROSE_MENTIONS", "INTERPRETER_REAL_WRITES",
    ],
}
HATCH = "  # allow: I really want to"


def _legacy_module(rel):
    spec = importlib.util.spec_from_file_location("legacy_" + Path(rel).stem, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _base_input(case):
    text = case["input"]
    if "variant:hatch" in case["tags"] and text.endswith(HATCH):
        text = text[: -len(HATCH)]
    return os.path.expanduser(text) if case["tool"] != "Bash" else text


# --------------------------------------------------------------------------
# One test per class
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cls,cases", CLASSES, ids=[c for c, _ in CLASSES])
def test_corpus_class_holds(cls, cases):
    bad = [(c["id"], c["input"], f) for c in cases for f in [cr.failures(c)] if f]
    assert not bad, bad


# --------------------------------------------------------------------------
# The data itself
# --------------------------------------------------------------------------


def test_corpus_is_valid_against_the_schema():
    assert CASES, "no corpus cases found"
    assert not cr.lint(CASES)


def test_lint_rejects_a_case_without_class_or_source():
    """Must-fail control for the lint: a lint that passes everything proves nothing."""
    good = CASES[0]
    missing = []
    for field in ("class", "source", "expect"):
        broken = {k: v for k, v in good.items() if k != field}
        if not any(field in p for p in cr.lint([broken])):
            missing.append(field)
    twin = [good, dict(good)]
    if not any("duplicate id" in p for p in cr.lint(twin)):
        missing.append("duplicate-id")
    assert not missing, f"lint accepted a case lacking: {missing}"


def test_every_negated_expectation_fails():
    """Must-fail control for the runner: if it stops reading a clause (a lost expectation),
    the contradiction of that clause passes and this goes red. Only cases that hold today are
    probed, so an engine regression is reported by its class test, not smeared across this one."""
    vacuous = [
        c["id"] for c in CASES
        if not cr.failures(c) and not cr.failures(c, cr.contradiction(c["expect"]))
    ]
    assert not vacuous, vacuous


# --------------------------------------------------------------------------
# Equivalence with today's pass set (one-time, until the legacy tables go)
# --------------------------------------------------------------------------


def test_corpus_reproduces_every_legacy_table_exactly():
    """Each legacy table entry is a corpus case under its own source, and each corpus
    case that names a legacy table names an entry that table really contains."""
    bad = []
    for rel, names in LEGACY_TABLES.items():
        mod = _legacy_module(rel)
        for name in names:
            table = [os.path.expanduser(x) for x in getattr(mod, name)]
            owned = [c for c in CASES if c["source"] == f"{rel}::{name}"]
            have = {_base_input(c) for c in owned}
            bad += [(name, "missing from corpus", x) for x in table if x not in have]
            bad += [(name, "not in legacy table", c["id"]) for c in owned if _base_input(c) not in table]
    dangling = [
        c["id"] for c in CASES
        if c["source"].split("::")[0] in LEGACY_TABLES
        and not hasattr(_legacy_module(c["source"].split("::")[0]), c["source"].split("::")[1])
        and not c["source"].split("::")[1].startswith("test_")
    ]
    assert not bad and not dangling, (bad, dangling)


def test_every_class_has_cases_on_both_sides_of_the_line():
    """False-positive classes are first class: the corpus needs allow-side cases, not only blocks."""
    positive = {c["class"] for c in CASES if "false-positive" in c["tags"]}
    assert {"safe", "danger-as-data", "operand-substitution", "control-plane-mention",
            "prose-mention", "agent-writable"} <= positive
