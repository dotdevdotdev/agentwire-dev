"""#1219: damage-control hooks register fail-closed (onFailure:block), gated on a passing selftest."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from agentwire import safety_commands as sc
from tests.unit.test_safety_heal import fake_env  # noqa: F401  (fixture)


def _owned(home):
    pre = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    return {e["matcher"]: e["hooks"][0] for e in pre if e["hooks"][0]["command"].startswith("~/.agentwire/")}


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


def test_heal_registers_without_block_when_the_selftest_fails(fake_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sc, "damage_control_selftest", lambda: ["bash-tool-damage-control.py: exit 1, expected 0"])
    summary = sc.heal_damage_control(quiet=True)
    assert summary["selftest_failures"]
    assert {h.get("onFailure") for h in _owned(fake_env).values()} == {None}

    monkeypatch.setattr(sc, "damage_control_selftest", lambda: [])
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


_GOOD = """#!/bin/sh
payload=$(cat)
case "$payload" in *"rm -rf"*) [ "$(basename "$0")" = bash-tool-damage-control.py ] && exit 2;; esac
exit 0
"""


def _fake_hooks(dir_: Path, overrides=None, mode=0o755):
    dir_.mkdir(parents=True, exist_ok=True)
    for name in set(sc.DAMAGE_CONTROL_MATCHERS.values()):
        f = dir_ / name
        f.write_text((overrides or {}).get(name, _GOOD))
        f.chmod(mode)


@pytest.mark.real_hook_probe
def test_selftest_catches_each_way_a_hook_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "HOOKS_DIR", tmp_path / "hooks")
    _fake_hooks(tmp_path / "hooks")
    assert sc.damage_control_selftest() == []

    cases = {
        "missing interpreter": {"edit-tool-damage-control.py": "#!/nonexistent/interp\n"},
        "crash": {"write-tool-damage-control.py": "#!/bin/sh\nexit 1\n"},
        "rules not loaded (bash allows rm -rf)": {"bash-tool-damage-control.py": "#!/bin/sh\ncat >/dev/null\nexit 0\n"},
    }
    missed = []
    for label, override in cases.items():
        _fake_hooks(tmp_path / "hooks", override)
        failures = sc.damage_control_selftest()
        if not any(next(iter(override)) in f for f in failures):
            missed.append(label)
    # a hook that lost its exec bit (what the read hook shipped with)
    _fake_hooks(tmp_path / "hooks")
    (tmp_path / "hooks" / "read-tool-damage-control.py").chmod(0o644)
    if not any("read-tool" in f for f in sc.damage_control_selftest()):
        missed.append("lost exec bit")
    assert not missed, missed


def test_every_bundled_hook_is_executable():
    hooks_dir = Path(sc.__file__).parent / "hooks" / "damage-control"
    not_exec = [n for n in set(sc.DAMAGE_CONTROL_MATCHERS.values())
                if not os.stat(hooks_dir / n).st_mode & stat.S_IXUSR]
    assert not not_exec, not_exec
