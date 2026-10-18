"""Every shipped rule/tooldef file must load, and a file that doesn't must say so (#1130, #1137).

``tooldefs/gws.yaml`` once shipped unparseable and the loader's bare
``except: continue`` dropped it without a word. gws policy is now user-level
(``docs/wiki/internals/examples/``), so these tests also drive that mechanism
through the real loader.
"""

from __future__ import annotations

import io
import shutil
from contextlib import redirect_stderr
from pathlib import Path

import yaml

from agentwire import safety_commands
from agentwire.safety._core import (
    check_command,
    load_config,
    load_safety_config,
    policy_file_parse_failures,
)

ROOT = Path(safety_commands.__file__).parent
RULES = ROOT / "hooks" / "damage-control" / "rules"
TOOLDEFS = ROOT / "tooldefs"
EXAMPLES = ROOT.parent / "docs" / "wiki" / "internals" / "examples"


def _load(rules: Path, tooldefs: Path):
    err = io.StringIO()
    with redirect_stderr(err):
        cfg = load_config(rules, tooldefs)
    return cfg, err.getvalue()


def _user_dirs(tmp_path: Path):
    """A user config: the bundled files installed, plus the gws example files."""
    rules, tooldefs = tmp_path / "damage-control", tmp_path / "tooldefs"
    shutil.copytree(RULES, rules)
    shutil.copytree(TOOLDEFS, tooldefs)
    shutil.copy(EXAMPLES / "gws-rules.yaml", rules / "gws.yaml")
    shutil.copy(EXAMPLES / "gws-tooldef.yaml", tooldefs / "gws.yaml")
    return rules, tooldefs


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


def test_unparseable_file_is_reported_with_file_and_line(tmp_path):
    rules, tooldefs = tmp_path / "rules", tmp_path / "tooldefs"
    rules.mkdir()
    tooldefs.mkdir()
    (rules / "ok.yaml").write_text("bashToolPatterns: []\n")
    (rules / "bad-rules.yaml").write_text("bashToolPatterns:\n  - pattern: a: b\n")
    (tooldefs / "bad-tool.yaml").write_text("name: x\ncommands:\n  - cmd: y\n    d: p: q\n")

    cfg, err = _load(rules, tooldefs)

    failures = {Path(f["file"]).name: f for f in cfg["_parse_failures"]}
    assert set(failures) == {"bad-rules.yaml", "bad-tool.yaml"}, failures
    assert failures["bad-rules.yaml"]["line"] == 2, failures
    assert failures["bad-tool.yaml"]["line"] == 4, failures
    for name in failures:
        assert f"{name}:" in err and "SKIPPED" in err, err


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


P = "--params '{\"userId\":\"me\"}'"

GWS_WRITES = [
    f"gws gmail users messages send {P}", "gws gmail users drafts send",
    "gws gmail users messages modify", "gws drive permissions create",
    "gws drive files create", "gws drive files trash", "gws calendar events patch",
    "gws calendar events quickAdd", "gws calendar acl insert", "gws sheets spreadsheets create",
    "gws docs documents create", "gws admin groups members insert",
    "gws gmail +send --to a@b.c", "gws gmail +insert", "gws gmail +upload",
    "gws sheets spreadsheets values batchUpdate", "gws gmail users messages untrash",
    "gws --format json gmail users messages send",
    "GWS_X=1 gws gmail users messages send", "ssh h 'gws gmail users messages send'",
    "bash -c 'gws drive files create'", "cd /x && gws drive files create",
    "echo hi; gws drive permissions create", "env FOO=1 gws drive files copy",
    "gws gmail users messages delete", "gws drive files delete",
    "gws calendar events delete", "gws-auth ~/.config/gws-work",
]

GWS_READS = [
    "gws auth status", f"gws gmail users messages list {P}", f"gws gmail users messages get {P}",
    "gws gmail users labels list", "gws gmail users getProfile", "gws gmail users threads get",
    "gws gmail users settings sendAs list", "gws gmail users messages attachments get",
    "gws calendar calendarList list", "gws calendar events list", "gws calendar events get",
    "gws calendar freebusy query", "gws drive files list", "gws drive files get",
    "gws drive files export", "gws drive permissions list", "gws sheets spreadsheets get",
    "gws sheets spreadsheets values get", "gws sheets spreadsheets values batchGet",
    "gws docs documents get", "gws admin users list", "gws admin users get",
    "gws schema gmail.users.messages.send", "gws gmail users messages send --dry-run",
    "gws drive permissions create --help", "gws help drive",
    "gws gmail users messages list --params '{\"q\":\"send create delete\"}'",
    "echo gws gmail users messages send", "grep -r 'gws drive files create' docs",
]


def test_bundled_set_carries_no_gws_policy():
    cfg, _ = _load(RULES, TOOLDEFS)
    got = {c: check_command(c, cfg)["decision"] for c in GWS_WRITES if not c.startswith("gws-auth")}
    assert set(got.values()) == {"allow"}, got


def test_user_authored_tooldef_and_rules_load_through_the_real_loader(tmp_path):
    """The documented user-level path: files dropped into the user dirs just load."""
    rules, tooldefs = _user_dirs(tmp_path)
    cfg, err = _load(rules, tooldefs)
    assert not cfg.get("_parse_failures") and "SKIPPED" not in err, (cfg.get("_parse_failures"), err)

    wrong = {}
    for cmd in GWS_WRITES:
        got = check_command(cmd, cfg)["decision"]
        if got not in ("ask", "block"):
            wrong[cmd] = ("ask|block", got)
    for cmd in GWS_READS:
        got = check_command(cmd, cfg)["decision"]
        if got != "allow":
            wrong[cmd] = ("allow", got)
    assert not wrong, wrong

    ids = {c: check_command(c, cfg).get("id") for c in ("gws calendar events insert", "gws calendar events quickAdd")}
    assert set(ids.values()) == {"gws.calendar-event-create"}, ids


def test_user_tooldef_parse_failure_is_reported_not_silent(tmp_path):
    rules, tooldefs = _user_dirs(tmp_path)
    (tooldefs / "gws.yaml").write_text("name: x\ncommands:\n  - cmd: y\n    description: a: b\n")
    cfg, err = _load(rules, tooldefs)
    assert [(Path(f["file"]).name, f["line"]) for f in cfg["_parse_failures"]] == [("gws.yaml", 4)], cfg["_parse_failures"]
    assert "gws.yaml:4" in err, err


def test_retired_gws_files_are_reported_quietly_and_never_deleted(tmp_path, monkeypatch):
    rules, tooldefs = tmp_path / "damage-control", tmp_path / "tooldefs"
    rules.mkdir()
    tooldefs.mkdir()
    manifest = safety_commands.load_rule_baselines()
    assert manifest["tooldefs"]["gws.yaml"] and manifest["rules"]["gws.yaml"]
    # The tooldef that #1134 healed onto machines must read as a shipped leftover.
    monkeypatch.setattr(safety_commands, "RULES_DIR", rules)
    monkeypatch.setattr(safety_commands, "TOOLDEFS_DIR", tooldefs)

    pristine_digest = manifest["tooldefs"]["gws.yaml"][0]
    stand_in = tooldefs / "gws.yaml"
    stand_in.write_text("# stand-in for a shipped release\n")
    shipped = safety_commands._sha256(stand_in)
    monkeypatch.setattr(
        safety_commands, "load_rule_baselines",
        lambda: {"rules": manifest["rules"], "tooldefs": {"gws.yaml": [shipped]}},
    )
    assert safety_commands.retired_installed()["tooldefs"] == {"gws.yaml": "pristine"}
    stand_in.write_text("# my own gws rules\n")
    assert safety_commands.retired_installed()["tooldefs"] == {"gws.yaml": "edited"}
    assert pristine_digest  # history retained

    monkeypatch.setattr(safety_commands, "load_rule_baselines", lambda: manifest)
    monkeypatch.setattr(safety_commands, "HOOKS_DIR", tmp_path / "hooks")
    monkeypatch.setattr(safety_commands, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(safety_commands, "register_damage_control_in_settings", lambda: 0)
    safety_commands.heal_damage_control(quiet=True, allow_foreign=True)
    assert stand_in.read_text() == "# my own gws rules\n"
    assert "gws.yaml" not in {p.name for p in TOOLDEFS.glob("*.yaml")}
