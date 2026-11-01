"""Every shipped rule/tooldef file must load, and a file that doesn't must say so (#1130, #1137).

``tooldefs/gws.yaml`` once shipped unparseable and the loader's bare
``except: continue`` dropped it without a word.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr
from pathlib import Path

import yaml

from agentwire import safety_commands
from damage_control.engine import (
    load_config,
    load_safety_config,
    policy_file_parse_failures,
)

ROOT = Path(safety_commands.__file__).parent
RULES = safety_commands.get_damage_control_source()
TOOLDEFS = ROOT / "tooldefs"


def _load(rules: Path, tooldefs: Path):
    err = io.StringIO()
    with redirect_stderr(err):
        cfg = load_config(rules, tooldefs)
    return cfg, err.getvalue()


def test_every_shipped_rule_and_tooldef_file_loads():
    bad = {}
    for path in sorted([*RULES.glob("*.yaml"), *TOOLDEFS.glob("*.yaml")]):
        try:
            data = yaml.safe_load(path.read_text())
            assert isinstance(data, dict), "top level is not a mapping"
        except Exception as exc:
            bad[path.name] = str(exc)
    assert not bad, bad

    cfg, err = _load(RULES, TOOLDEFS)
    assert not cfg.get("_parse_failures"), cfg.get("_parse_failures")
    assert "SKIPPED" not in err, err


def test_project_and_global_policy_parse_failures_are_reported_and_stay_fail_secure(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".damagecontrol.yml").write_text("enabled: true\nx: y: z\n")
    good = tmp_path / "global.yml"
    good.write_text("enabled: true\n")
    bad_global = tmp_path / "bad-global.yml"
    bad_global.write_text("enabled: [\n")

    only_project = policy_file_parse_failures(good, str(proj))
    assert [(Path(f["file"]).name, f["line"]) for f in only_project] == [(".damagecontrol.yml", 2)], only_project
    both = policy_file_parse_failures(bad_global, str(proj))
    assert {Path(f["file"]).name for f in both} == {"bad-global.yml", ".damagecontrol.yml"}, both

    err = io.StringIO()
    with redirect_stderr(err):
        cfg = load_safety_config(bad_global, str(proj))
    assert cfg["enabled"] is True
    assert err.getvalue().count("SKIPPED") == 2, err.getvalue()


