"""The installer and update model of the damage_control package (#1209 M5).

Everything runs against a temp HOME: nothing here may touch the real ``~/.claude``. The
self-test and Claude Code version probes are stubbed except in the tests that are ABOUT them.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from damage_control import claude_code as cc
from damage_control import cli, engine, install, ruleset
from damage_control import harness_config as hc
from damage_control.profile import GENERIC

GOOD_CLAUDE = (2, 1, 316)
REAL_SELFTEST = install.selftest


@pytest.fixture
def restore_profile():
    """Put the process's damage-control profile back.

    `install_profile` rewrites module-level derived state (protected paths, ...), and the CLI's
    --profile calls it, so monkeypatch cannot undo it. Tests here switch profiles with
    `install_profile`, never `monkeypatch.setattr(engine, "PROFILE", ...)`: the suite's
    monkeypatch tears down AFTER this fixture and would put a stale PROFILE back.
    """
    previous = engine.PROFILE
    yield
    engine.install_profile(previous)


@pytest.fixture
def env(restore_profile, tmp_path, monkeypatch):
    """A temp HOME, the generic profile, a known Claude Code version, and no real self-test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DAMAGE_CONTROL_HOME", raising=False)
    engine.install_profile(GENERIC)
    monkeypatch.setattr(cc, "detect_version", lambda: GOOD_CLAUDE)
    monkeypatch.setattr(install, "install_source", lambda: "pypi")
    monkeypatch.setattr(install, "selftest", lambda interpreter, profile=None: [])
    settings = home / ".claude" / "settings.json"
    return type("Env", (), {"home": home, "settings": settings, "tmp": tmp_path})


def run_cli(*argv):
    return cli.main(list(argv))


def write(path: Path, data) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data if isinstance(data, bytes) else data.encode()
    path.write_bytes(raw)
    return raw


def registered(settings: Path):
    return json.loads(settings.read_text())["hooks"]["PreToolUse"]


# ---------------------------------------------------------------------------
# harness_config: the malformed-settings corpus
# ---------------------------------------------------------------------------

UNPARSEABLE = {
    "trailing-comma": b'{"a": 1,}',
    "truncated": b'{"a": ',
    "not-an-object": b'[1, 2]',
    "bare-string": b'"settings"',
    "nan-constant": b'{"a": NaN}',
    "binary": b"\xff\xfe\x00garbage",
    "comments": b'{"a": 1} // comment',
}


def test_an_unparseable_settings_file_is_refused_byte_identical_with_nonzero_exit(env):
    """The #1220 shape: `except: settings = {}` replaced the user's file with ours."""
    bad = []
    for name, raw in UNPARSEABLE.items():
        write(env.settings, raw)
        code = run_cli("init")
        if code == 0 or env.settings.read_bytes() != raw:
            bad.append((name, code, env.settings.read_bytes()))
        leftovers = [p.name for p in env.settings.parent.iterdir() if p.name != "settings.json"]
        if leftovers:
            bad.append((name, "leftover files", leftovers))
    assert not bad, bad


def test_formatting_survives_an_edit(env):
    """BOM, CRLF, a 4-space indent and a missing trailing newline are the user's, not ours."""
    bad = []
    cases = {
        "bom": b'\xef\xbb\xbf{\n  "model": "x"\n}\n',
        "crlf": b'{\r\n  "model": "x"\r\n}\r\n',
        "indent4": b'{\n    "model": "x"\n}\n',
        "no-trailing-newline": b'{\n  "model": "x"\n}',
    }
    for name, raw in cases.items():
        write(env.settings, raw)
        assert run_cli("init") == 0, name
        out = env.settings.read_bytes()
        style, want = hc.detect_style(out), hc.detect_style(raw)
        if style != want:
            bad.append((name, style, want))
        if json.loads(out.decode("utf-8-sig"))["model"] != "x":
            bad.append((name, "lost a key"))
    assert not bad, bad


def test_unknown_keys_and_foreign_hooks_survive_init_and_uninstall(env):
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/true"}]}
    original = {"theme": "dark", "permissions": {"allow": ["Bash(ls)"]}, "future": {"x": [1, 2]},
                "hooks": {"PreToolUse": [foreign], "PostToolUse": [{"matcher": "*", "hooks": []}]}}
    write(env.settings, json.dumps(original, indent=2) + "\n")
    assert run_cli("init") == 0
    after = json.loads(env.settings.read_text())
    assert {k: v for k, v in after.items() if k != "hooks"} == {k: v for k, v in original.items() if k != "hooks"}
    assert after["hooks"]["PostToolUse"] == original["hooks"]["PostToolUse"]
    assert foreign in after["hooks"]["PreToolUse"]
    # The user edits the file after init, so uninstall cannot just restore bytes.
    after["theme"] = "light"
    env.settings.write_text(json.dumps(after, indent=2) + "\n")
    assert run_cli("uninstall") == 0
    assert json.loads(env.settings.read_text()) == dict(original, theme="light")


def test_read_only_file_is_refused_untouched(env):
    raw = write(env.settings, b'{"model": "x"}\n')
    env.settings.chmod(0o444)
    try:
        assert run_cli("init") == 1
    finally:
        env.settings.chmod(0o644)
    assert env.settings.read_bytes() == raw


def test_a_symlinked_settings_file_is_edited_through_the_link(env):
    target = env.tmp / "dotfiles" / "settings.json"
    write(target, b'{"model": "x"}\n')
    env.settings.parent.mkdir(parents=True)
    env.settings.symlink_to(target)
    assert run_cli("init") == 0
    assert env.settings.is_symlink() and env.settings.resolve() == target.resolve()
    assert "hooks" in json.loads(target.read_text())


def test_empty_and_absent_files_get_a_fresh_config_and_uninstall_removes_what_init_created(env):
    write(env.settings, b"")
    assert run_cli("init") == 0 and registered(env.settings)
    assert run_cli("uninstall") == 0
    assert not env.settings.exists() or env.settings.read_bytes() == b""
    env.settings.unlink(missing_ok=True)
    assert run_cli("init") == 0 and run_cli("uninstall") == 0
    assert not env.settings.exists()


def test_malformed_hooks_tree_is_refused_not_repaired(env):
    bad = []
    for name, hooks in {"hooks-list": [], "pretooluse-dict": {"PreToolUse": {}}}.items():
        raw = write(env.settings, json.dumps({"hooks": hooks}))
        if run_cli("init") == 0 or env.settings.read_bytes() != raw:
            bad.append(name)
    assert not bad, bad


KILL_CHILD = textwrap.dedent("""
    import os, signal, sys
    from pathlib import Path
    from damage_control import harness_config as hc
    point = sys.argv[2]
    if point == "before-replace":
        os.replace = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
    else:
        hc._fsync_dir = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
    hc.edit_json_config(Path(sys.argv[1]), lambda d: d.update(new="value") or True)
""")


def test_kill_9_mid_write_leaves_the_old_file_or_the_new_one_never_a_partial(env):
    """SIGKILL at the two edges of the atomic replace."""
    old = b'{"keep": "me"}\n'
    bad = []
    for point in ("before-replace", "after-replace"):
        write(env.settings, old)
        proc = subprocess.run([sys.executable, "-I", "-c", KILL_CHILD, str(env.settings), point])
        got = env.settings.read_bytes()
        if proc.returncode != -signal.SIGKILL:
            bad.append((point, "child was not killed", proc.returncode))
        try:
            parsed = json.loads(got)
        except ValueError:
            bad.append((point, "partial file", got))
            continue
        want_new = point == "after-replace"
        if (parsed.get("new") == "value") != want_new or parsed.get("keep") != "me":
            bad.append((point, got))
    assert not bad, bad


def test_a_concurrent_writer_is_detected_and_never_clobbered(env, monkeypatch):
    """The file changes between our read and our replace, every time: refuse, writing nothing."""
    write(env.settings, b'{"a": 1}\n')
    real = hc.read_bytes
    calls = {"n": 0}

    def racing(path):
        data = real(path)
        calls["n"] += 1
        env.settings.write_bytes(json.dumps({"a": calls["n"]}).encode())  # someone else writes after every read
        return data

    monkeypatch.setattr(hc, "read_bytes", racing)
    with pytest.raises(hc.ConfigError, match="kept changing"):
        hc.edit_json_config(env.settings, lambda d: d.update(b=2) or True)
    monkeypatch.setattr(hc, "read_bytes", real)
    assert "b" not in json.loads(env.settings.read_text())


# ---------------------------------------------------------------------------
# init / status / uninstall
# ---------------------------------------------------------------------------

def test_init_twice_changes_nothing(env):
    write(env.settings, b'{"model": "x"}\n')
    assert run_cli("init") == 0
    first = env.settings.read_bytes(), (env.home / ".damage-control/state/install.json").read_bytes()
    mtime = env.settings.stat().st_mtime_ns
    assert run_cli("init") == 0
    assert (env.settings.read_bytes(), (env.home / ".damage-control/state/install.json").read_bytes()) == first
    assert env.settings.stat().st_mtime_ns == mtime  # not even rewritten


def test_uninstall_restores_the_original_bytes_when_the_file_is_still_ours(env):
    original = b'{\r\n    "model": "x",\r\n    "hooks": {}\r\n}'
    write(env.settings, original)
    assert run_cli("init") == 0
    assert env.settings.read_bytes() != original
    assert run_cli("uninstall") == 0
    assert env.settings.read_bytes() == original
    assert not (env.home / ".damage-control/state/install.json").exists()


def test_a_repair_after_user_edits_never_restores_stale_bytes(env):
    """Byte-restore is only valid while the file is exactly what our writes produced."""
    write(env.settings, b'{"model": "x"}\n')
    assert run_cli("init") == 0
    data = json.loads(env.settings.read_text())
    data["added_by_user"] = True
    data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 1  # drift, so init rewrites
    env.settings.write_text(json.dumps(data, indent=2) + "\n")
    assert run_cli("init") == 0
    assert run_cli("uninstall") == 0
    assert json.loads(env.settings.read_text()).get("added_by_user") is True


def findings_by_code(env):
    return {f.code for f in install.inspect("user") if f.level == "bad"}


def mutate_settings(env, fn):
    data = json.loads(env.settings.read_text())
    fn(data["hooks"]["PreToolUse"])
    env.settings.write_text(json.dumps(data, indent=2) + "\n")


def test_status_detects_each_injected_drift(env, monkeypatch):
    assert run_cli("init") == 0
    assert run_cli("status") == 0

    def drop_bash(pre):
        pre[:] = [e for e in pre if e["matcher"] != "Bash"]

    def dead_interpreter(pre):
        for e in pre:
            e["hooks"][0]["command"] = "/nonexistent/python"

    def wrong_matcher(pre):
        pre[0]["matcher"] = "Bsh"

    def lose_timeout(pre):
        del pre[0]["hooks"][0]["timeout"]

    def lose_onfailure(pre):
        del pre[0]["hooks"][0]["onFailure"]

    expected = {
        "missing entry": (drop_bash, "missing"),
        "dead interpreter": (dead_interpreter, "interpreter"),
        "wrong matcher": (wrong_matcher, "matcher"),
        "lost timeout": (lose_timeout, "timeout"),
        "lost onFailure": (lose_onfailure, "onFailure"),
    }
    missed = []
    for name, (inject, code) in expected.items():
        assert run_cli("init") == 0  # repair to a known-good state first
        mutate_settings(env, inject)
        if run_cli("status") != 1 or code not in findings_by_code(env):
            missed.append((name, findings_by_code(env)))
    assert not missed, missed


def test_status_detects_a_stale_ruleset_and_refresh_acknowledges_it(env, monkeypatch, capsys):
    assert run_cli("init") == 0
    record_file = env.home / ".damage-control/state/install.json"
    record = json.loads(record_file.read_text())
    rid = sorted(record["rules"])[0]
    record["rules"][rid] = "ask:0000000000"  # as if the shipped rule differed when last acknowledged
    record_file.write_text(json.dumps(record))
    assert "stale-ruleset" in {f.code for f in install.inspect("user")}
    assert run_cli("init", "--refresh") == 0
    assert "stale-ruleset" not in {f.code for f in install.inspect("user")}


def test_refresh_marks_every_loosening_and_a_new_default_grant(env, monkeypatch, capsys):
    assert run_cli("init") == 0
    record_file = env.home / ".damage-control/state/install.json"
    record = json.loads(record_file.read_text())
    ask = next(r for r, v in record["rules"].items() if v.startswith("ask:"))
    record["rules"]["invented.removed-rule"] = "block:abc"  # in the record, gone from the bundle: removed
    record["rules"][ask] = "block:" + record["rules"][ask].split(":")[1]  # was stricter, now ask: lowered
    record["default_grants"] = []
    record_file.write_text(json.dumps(record))
    engine.install_profile(GENERIC._replace(default_unattended_allow=("git.commit",)))
    capsys.readouterr()
    assert run_cli("init", "--refresh") == 0
    out = capsys.readouterr().out
    lines = {line.strip() for line in out.splitlines() if line.strip().startswith("!!")}
    want = {"!! - invented.removed-rule (was block)", f"!! ~ {ask} block -> ask", "!! + default unattended grant git.commit"}
    assert want <= lines, (want - lines, out)


def test_ruleset_diff_directions():
    old = {"a": "block:1", "b": "ask:2", "c": "block:3", "allowedPaths:/x": "allow:-"}
    new = {"a": "ask:1", "b": "ask:2", "d": "block:4", "allowedPaths:/y": "allow:-"}
    got = {c.text: c.loosening for c in ruleset.diff(old, new, ["g1"], ["g1", "g2"])}
    assert got == {
        "~ a block -> ask": True,                      # lowered tier
        "- c (was block)": True,                       # removed protection
        "+ d (block)": False,                          # a new protection is not loosening
        "+ allowedPaths:/y (allow) widens what is allowed": True,
        "- allowedPaths:/x (was allow)": False,        # removing an allow tightens
        "+ default unattended grant g2": True,
    }


def test_downgrade_is_refused_without_force(env):
    assert run_cli("init") == 0
    record_file = env.home / ".damage-control/state/install.json"
    record = json.loads(record_file.read_text())
    record["package_version"] = "99.0.0"
    record_file.write_text(json.dumps(record))
    before = env.settings.read_bytes()
    assert run_cli("init") == 1 and env.settings.read_bytes() == before
    assert run_cli("init", "--force") == 0


def test_claude_code_below_the_floor_is_refused_not_registered_weaker(env, monkeypatch):
    bad = []
    for version in ((2, 1, 294), (1, 0, 0), None):
        monkeypatch.setattr(cc, "detect_version", lambda v=version: v)
        if run_cli("init") != 1 or env.settings.exists():
            bad.append(version)
    assert not bad, bad


def test_fail_open_is_explicit_recorded_and_registers_without_onfailure(env, monkeypatch):
    monkeypatch.setattr(cc, "detect_version", lambda: (2, 1, 1))
    assert run_cli("init", "--fail-open") == 0
    assert all("onFailure" not in e["hooks"][0] for e in registered(env.settings))
    assert "fail_open: true" in (env.home / ".damage-control/damagecontrol.yml").read_text()
    assert json.loads((env.home / ".damage-control/state/install.json").read_text())["harnesses"]["claude-code:user"]["fail_mode"] == "open"
    assert run_cli("status") == 0  # status holds it to the mode it was installed in


def test_dev_sources_cannot_write_machine_global_state_without_the_flag(env, monkeypatch):
    bad = []
    for source in install.DEV_SOURCES:
        monkeypatch.setattr(install, "install_source", lambda s=source: s)
        if run_cli("init") != 1 or env.settings.exists():
            bad.append(source)
    assert not bad, bad
    assert run_cli("init", "--allow-dev-source") == 0


def test_a_failed_self_test_registers_nothing(env, monkeypatch):
    monkeypatch.setattr(install, "selftest", lambda interpreter, profile=None: ["bash: exit 1, expected 2"])
    assert run_cli("init") == 1
    assert not env.settings.exists() and not (env.home / ".damage-control/state/install.json").exists()


def test_the_real_self_test_passes_and_fails_on_a_dead_interpreter(env):
    """Unstubbed: the exec-form command, in a subprocess, proves the rules loaded."""
    assert REAL_SELFTEST(sys.executable) == []
    assert REAL_SELFTEST("/nonexistent/python"), "a dead interpreter must fail the self-test"


def test_init_refuses_entries_another_installer_owns(env):
    """A host (agentwire) registers the same module with --profile; a profile-less init must not rewrite it."""
    write(env.settings, json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": sys.executable,
                                       "args": ["-I", "-m", "damage_control.hook", "bash", "--profile", "/x/profile.json"]}]}]}}))
    raw = env.settings.read_bytes()
    assert run_cli("init") == 1
    assert env.settings.read_bytes() == raw


def test_a_host_profile_is_registered_and_not_reported_as_drift(env, tmp_path):
    from damage_control.profile import dump_profile

    manifest = tmp_path / "profile.json"
    manifest.write_text(dump_profile(GENERIC))
    assert run_cli("init", "--profile", str(manifest)) == 0
    assert all("--profile" in e["hooks"][0]["args"] for e in registered(env.settings))
    assert run_cli("status", "--profile", str(manifest)) == 0
    assert run_cli("init") == 1  # a profile-less init refuses to take the entries over


def test_project_scope_manages_the_project_settings_file(env, monkeypatch):
    project = env.tmp / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    assert run_cli("init", "--scope", "project") == 0
    assert registered(project / ".claude" / "settings.json") and not env.settings.exists()


def test_managed_scope_is_a_dry_run_unless_applied(env, tmp_path, capsys):
    target = tmp_path / "managed" / "50-damage-control.json"
    assert run_cli("init", "--scope", "managed", "--settings", str(target)) == 0
    assert not target.exists() and "dry run" in capsys.readouterr().out
    assert run_cli("init", "--scope", "managed", "--settings", str(target), "--apply") == 0
    assert registered(target)


def test_dry_run_writes_nothing(env, capsys):
    write(env.settings, b'{"model": "x"}\n')
    raw = env.settings.read_bytes()
    assert run_cli("init", "--dry-run") == 0
    assert env.settings.read_bytes() == raw and not (env.home / ".damage-control").exists()
    assert "+++" in capsys.readouterr().out


def test_breakglass_unregisters_with_the_stdlib_only_script(env):
    assert run_cli("init") == 0
    script = env.home / ".damage-control/bin/damage-control-breakglass"
    assert script.exists()
    done = subprocess.run([sys.executable, "-I", str(script), "unregister"], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(env.home)})
    assert done.returncode == 0, done.stderr
    assert not cc.owned_entries(json.loads(env.settings.read_text()))
