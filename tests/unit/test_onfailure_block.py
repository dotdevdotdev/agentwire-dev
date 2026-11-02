"""#1219: damage-control hooks register fail-closed (onFailure:block), gated on a passing selftest."""

import json
import subprocess
from pathlib import Path

import pytest

from agentwire import safety_commands as sc
from tests.unit.test_safety_heal import fake_env  # noqa: F401  (fixture)


def _owned(home):
    pre = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    return {e["matcher"]: e["hooks"][0] for e in pre if sc._is_ours(e["hooks"][0])}


def test_every_registered_matcher_fails_closed_and_upgrade_repairs_old_installs(fake_env):  # noqa: F811
    sc.register_damage_control_in_settings()
    owned = _owned(fake_env)
    assert set(owned) == set(sc.DAMAGE_CONTROL_MATCHERS)
    assert {m for m, h in owned.items() if h.get("onFailure") != "block"} == set()
    assert sc.damage_control_onfailure_drift() == []

    # a pre-#1219 install: timeout only, plus a foreign hook that must be left alone
    path = fake_env / ".claude" / "settings.json"
    settings = json.loads(path.read_text())
    for e in settings["hooks"]["PreToolUse"]:
        for h in e["hooks"]:
            h.pop("onFailure")
    settings["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": "~/x.sh"}]})
    path.write_text(json.dumps(settings))
    assert set(sc.damage_control_onfailure_drift()) == set(sc.DAMAGE_CONTROL_MATCHERS)

    assert sc.register_damage_control_in_settings() == len(sc.DAMAGE_CONTROL_MATCHERS)
    assert sc.damage_control_onfailure_drift() == []
    foreign = [h for e in json.loads(path.read_text())["hooks"]["PreToolUse"] for h in e["hooks"]
               if h["command"] == "~/x.sh"]
    assert foreign == [{"type": "command", "command": "~/x.sh"}]


def test_not_fail_closed_never_adds_onfailure(fake_env):  # noqa: F811
    sc.register_damage_control_in_settings(fail_closed=False)
    assert {h.get("onFailure") for h in _owned(fake_env).values()} == {None}


def test_heal_switches_nothing_when_the_selftest_fails(fake_env, monkeypatch):  # noqa: F811
    """A block on top of a broken install bricks the session, so an unproven hook is never registered."""
    monkeypatch.setattr(sc, "damage_control_selftest", lambda interpreter=None: ["bash: exit 1, expected 0"])
    summary = sc.heal_damage_control(quiet=True)
    assert summary["selftest_failures"] and summary["matchers_added"] == 0
    assert not (fake_env / ".claude" / "settings.json").exists()

    monkeypatch.setattr(sc, "damage_control_selftest", lambda interpreter=None: [])
    sc.heal_damage_control(quiet=True)
    assert {h.get("onFailure") for h in _owned(fake_env).values()} == {"block"}


def test_heal_warns_loudly_on_a_claude_that_fails_open(fake_env, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(sc, "claude_code_version", lambda: (2, 1, 294))
    summary = sc.heal_damage_control(quiet=False)
    assert "FAILS OPEN" in capsys.readouterr().out
    assert summary["claude_code_version"] == "2.1.294"
    # installed anyway
    assert {h.get("onFailure") for h in _owned(fake_env).values()} == {"block"}


def test_onfailure_support_boundary():
    got = {v: sc.onfailure_supported(v) for v in [(2, 1, 294), (2, 1, 295), (2, 2, 0), (1, 9, 999), None]}
    assert got == {(2, 1, 294): False, (2, 1, 295): True, (2, 2, 0): True, (1, 9, 999): False, None: None}


@pytest.mark.real_hook_probe
def test_claude_version_probe_parses_real_output(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "2.1.316 (Claude Code)\n", ""))
    assert sc.claude_code_version() == (2, 1, 316)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "garbage", ""))
    assert sc.claude_code_version() is None

    def missing(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "run", missing)
    assert sc.claude_code_version() is None


# A stand-in interpreter: ``<script> -I -m damage_control.hook <kind> --profile <manifest>``, so $4 is the kind.
_GOOD = """#!/bin/sh
payload=$(cat)
case "$payload" in *"rm -rf"*) [ "$4" = bash ] && exit 2;; esac
exit 0
"""


def _interpreter(dir_: Path, body: str = _GOOD, mode: int = 0o755) -> str:
    dir_.mkdir(parents=True, exist_ok=True)
    f = dir_ / "python-standin"
    f.write_text(body)
    f.chmod(mode)
    return str(f)


@pytest.mark.real_hook_probe
def test_selftest_catches_each_way_a_hook_fails_open(tmp_path):
    assert sc.damage_control_selftest(_interpreter(tmp_path)) == []

    cases = {
        # label: (interpreter, a kind that must be named in a failure)
        "missing interpreter": ("/nonexistent/interp", "bash"),
        "crash": (_interpreter(tmp_path / "crash", '#!/bin/sh\ncat >/dev/null\n[ "$4" = write ] && exit 1\nexit 0\n'), "write"),
        "rules not loaded (bash allows rm -rf)": (_interpreter(tmp_path / "allow", "#!/bin/sh\ncat >/dev/null\nexit 0\n"), "bash"),
        "lost exec bit": (_interpreter(tmp_path / "noexec", mode=0o644), "read"),
    }
    missed = [label for label, (interp, kind) in cases.items()
              if not any(f.startswith(kind + ":") for f in sc.damage_control_selftest(interp))]
    assert not missed, missed


def test_every_registration_names_an_absolute_interpreter(fake_env):  # noqa: F811
    """A bare ``python`` in a registration resolves through whatever PATH the harness has: not a guard."""
    sc.register_damage_control_in_settings()
    relative = {m: h["command"] for m, h in _owned(fake_env).items() if not Path(h["command"]).is_absolute()}
    assert not relative, relative
