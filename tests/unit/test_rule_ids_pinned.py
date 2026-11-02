"""Rule ids are frozen (#1209 M1): every rule pins its id, and the id set is a golden file.

A derived id comes from the ``reason:`` text, so a typo fix used to rename it and
silently break ``disabled_rules`` / ``unattended_allow`` / audit queries. Changing
the id set is now a deliberate act: edit the rule, regenerate ``corpus/golden/rule_ids.json``
(see the ids-changelog policy in docs/wiki/internals/damage-control.md) and say so in the PR.
"""

from __future__ import annotations

import json
from pathlib import Path

from damage_control.engine import (
    DEFAULT_UNATTENDED_ALLOW,
    bundled_rule_sources,
    bundled_tooldefs_dir,
    load_config,
    parse_unattended_allow,
)

from agentwire.safety.lint import unpinned_rules

ROOT = Path(__file__).resolve().parent.parent.parent
RULES = bundled_rule_sources()
TOOLDEFS = bundled_tooldefs_dir()
GOLDEN = ROOT / "corpus" / "golden" / "rule_ids.json"


def _loaded_ids() -> list[str]:
    cfg = load_config(RULES, TOOLDEFS)
    return sorted(
        p["id"] for key in ("bashToolPatterns", "mcpToolPolicies") for p in cfg[key]
    )


def test_every_bundled_rule_pins_its_id():
    assert not unpinned_rules(RULES, TOOLDEFS), unpinned_rules(RULES, TOOLDEFS)


def test_lint_rejects_an_unpinned_pattern(tmp_path):
    rules, tooldefs = tmp_path / "rules", tmp_path / "tooldefs"
    rules.mkdir()
    tooldefs.mkdir()
    (rules / "x.yaml").write_text(
        "bashToolPatterns:\n  - pattern: '\\bfoo\\b'\n    reason: foo is bad\n"
        "  - id: x.pinned\n    pattern: '\\bbar\\b'\n    reason: bar\n"
    )
    (tooldefs / "t.yaml").write_text(
        "name: T\ncommands:\n  - cmd: 't go'\n    access: write\n    description: go\n"
        "  - cmd: 't look'\n    access: read\n    description: look\n"
    )
    assert unpinned_rules(rules, tooldefs) == ["x.yaml: foo is bad", "t.yaml: t go"]


def test_rule_ids_match_the_golden_file():
    ids = _loaded_ids()
    golden = json.loads(GOLDEN.read_text())
    assert ids == golden, {
        "removed": sorted(set(golden) - set(ids)),
        "added": sorted(set(ids) - set(golden)),
    }


def test_no_duplicate_rule_ids():
    ids = _loaded_ids()
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    assert not load_config(RULES, TOOLDEFS).get("_duplicate_rule_ids")


def test_every_default_unattended_allow_id_resolves_to_a_loaded_rule():
    known = set(_loaded_ids())
    wanted, errors = parse_unattended_allow(DEFAULT_UNATTENDED_ALLOW)
    assert not errors, errors
    missing = sorted(rid for rid in wanted if rid not in known)
    assert not missing, missing
