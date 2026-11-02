"""docs/coverage.md is generated from the hooks' own tables, so the README claim cannot drift (#1209 M6)."""

import importlib.util
from pathlib import Path

from damage_control import claude_code

TOOL = Path(__file__).resolve().parents[2] / "packages" / "damage-control-dev" / "tools" / "coverage_matrix.py"


def _tool():
    spec = importlib.util.spec_from_file_location("coverage_matrix", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_coverage_md_is_up_to_date():
    assert _tool().main(["--check"]) == 0, "run: uv run python packages/damage-control-dev/tools/coverage_matrix.py"


def test_every_registered_matcher_has_a_row():
    text = _tool().render()
    missing = [m for m in claude_code.MATCHERS if f"| `{m}` |" not in text]
    assert not missing, missing
