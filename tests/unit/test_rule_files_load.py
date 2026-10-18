"""Every shipped rule/tooldef file must load, and a file that doesn't must say so (#1130).

``tooldefs/gws.yaml`` shipped unparseable (``format: full|…`` inside a plain
scalar) and the loader's bare ``except: continue`` dropped it without a word, so
every gws rule was silently absent.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr
from pathlib import Path

import yaml

from agentwire import safety_commands
from agentwire.safety._core import check_command, load_config

ROOT = Path(safety_commands.__file__).parent
RULES = ROOT / "hooks" / "damage-control" / "rules"
TOOLDEFS = ROOT / "tooldefs"

# Pristine pre-fix gws.yaml: what an installed machine carries today.
BROKEN_GWS_TOOLDEF_SHA = "da7f3e1f7a3edeb5effd57b26a471d98b969931cced3beb919ddac92d6c1964b"


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


def test_gws_rules_are_live_and_judge_real_spelling():
    """The gws block rules used dotted names; the CLI is space-separated."""
    cfg, _ = _load(RULES, TOOLDEFS)
    wrong = {}
    expect = {
        "gws gmail users messages delete --params '{\"userId\":\"me\",\"id\":\"1\"}'": "block",
        "gws gmail users messages batchDelete": "block",
        "gws drive files delete --params '{\"fileId\":\"1\"}'": "block",
        "gws calendar calendars delete --params '{\"calendarId\":\"x\"}'": "block",
        "gws admin users delete --params '{\"userKey\":\"a@b.c\"}'": "block",
        "gws gmail users messages trash --params '{\"userId\":\"me\",\"id\":\"1\"}'": "ask",
        "gws calendar events insert --params '{\"calendarId\":\"primary\"}'": "ask",
        "gws sheets spreadsheets values update --params '{}'": "ask",
        "gws-auth ~/.config/gws-work": "ask",
        # read-only: must pass untouched (a wrongful ask is a silent loop unattended)
        "gws auth status": "allow",
        "gws gmail users messages list --params '{\"userId\":\"me\",\"q\":\"is:unread\"}'": "allow",
        "gws gmail users messages get --params '{\"userId\":\"me\",\"id\":\"1\",\"format\":\"full\"}'": "allow",
        "gws gmail users labels list --params '{\"userId\":\"me\"}'": "allow",
        "gws calendar events list --params '{\"calendarId\":\"primary\"}'": "allow",
        "gws drive files list --params '{\"q\":\"name contains x\"}'": "allow",
        "gws sheets spreadsheets values get --params '{}'": "allow",
        "gws docs documents get --params '{}'": "allow",
        "gws admin users list": "allow",
    }
    for cmd, want in expect.items():
        got = check_command(cmd, cfg)
        decision = got[0] if isinstance(got, tuple) else got["decision"]
        if decision != want:
            wrong[cmd] = (want, decision)
    assert not wrong, wrong


def test_installed_broken_gws_tooldef_is_outdated_and_healed(monkeypatch, tmp_path):
    """A machine holding the pre-fix gws.yaml must heal, not read as hand-edited."""
    manifest = safety_commands.load_rule_baselines()
    assert BROKEN_GWS_TOOLDEF_SHA in manifest["tooldefs"]["gws.yaml"]

    live = tmp_path / "tooldefs"
    live.mkdir()
    target = live / "gws.yaml"
    target.write_text("commands:\n  - description: x: y\n")
    stand_in = safety_commands._sha256(target)
    monkeypatch.setattr(
        safety_commands, "load_rule_baselines",
        lambda: {"rules": {}, "tooldefs": {"gws.yaml": [stand_in]}},
    )
    source = TOOLDEFS / "gws.yaml"
    state = safety_commands._sync_state(target, source, [stand_in])
    assert state == "outdated", state
