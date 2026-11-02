"""The hook reads the committed JSON; YAML is the authored form. They must not drift (#1209 M4).

Committed JSON beside its YAML source was chosen over a build hook because it works identically
for an editable checkout, an sdist, a wheel and ``uv tool install`` with nothing to run at install
time -- and its one failure mode, an edited YAML whose JSON was not regenerated (the hook would
enforce the OLD rule), is exactly what this test makes loud.
"""

import importlib.util
from pathlib import Path

from damage_control import engine

REPO = Path(__file__).resolve().parent.parent.parent
TOOL = REPO / "packages" / "damage-control-dev" / "tools" / "build_rules_json.py"


def _tool():
    spec = importlib.util.spec_from_file_location("build_rules_json", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_rule_and_tooldef_json_matches_its_yaml_source():
    dirs = [engine.bundled_rules_dir(), engine.bundled_tooldefs_dir(), REPO / "agentwire" / "safety" / "rules"]
    problems = _tool().sync(dirs, check=True)
    assert not problems, f"{problems}\nrun: uv run --script packages/damage-control-dev/tools/build_rules_json.py {' '.join(map(str, dirs))}"


def test_the_check_bites_on_a_stale_json_a_missing_json_and_an_orphan(tmp_path):
    """Must-fail control: if the drift check passed these, the test above would prove nothing."""
    tool = _tool()
    (tmp_path / "a.yaml").write_text("bashToolPatterns: []\n")
    assert tool.sync([tmp_path], check=True) == [f"{tmp_path / 'a.json'}: missing"]
    tool.sync([tmp_path], check=False)
    assert tool.sync([tmp_path], check=True) == []
    (tmp_path / "a.yaml").write_text("bashToolPatterns: [{pattern: x, reason: y}]\n")
    assert tool.sync([tmp_path], check=True) == [f"{tmp_path / 'a.json'}: out of date"]
    (tmp_path / "b.json").write_text("{}\n")
    assert any("no YAML source" in p for p in tool.sync([tmp_path], check=True))


def test_the_hook_enforces_the_json_not_the_yaml(tmp_path):
    """The loader prefers <stem>.json: a rule only the YAML carries is NOT in force."""
    (tmp_path / "x.yaml").write_text("bashToolPatterns:\n  - id: x.yaml-only\n    pattern: zzz\n    reason: r\n")
    (tmp_path / "x.json").write_text('{"bashToolPatterns": []}\n')
    assert engine.load_config(tmp_path)["bashToolPatterns"] == []
