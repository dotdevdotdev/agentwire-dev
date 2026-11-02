"""One integration test per extension point of the damage-control package (#1209 M6, section 4.2).

AgentWire's whole surface is ``safety_ext.build_profile()``. Each test drives the REAL hook
(``python -I -m damage_control.hook <kind> --profile <manifest>``) twice over the same payload:
once with AgentWire's manifest and once with the GENERIC profile's. The pair is the point: the
generic engine allows what AgentWire's profile gates, so a pass proves the profile field is what
changed the decision, not some rule the engine already had. Removing the field from
``build_profile`` turns each test red (mutation-checked, see the PR).
"""

import json
import os
import subprocess
import sys
import time

import pytest
from damage_control.profile import GENERIC, dump_profile

from agentwire import safety_ext


@pytest.fixture
def world(tmp_path):
    """A temp HOME plus a way to run any hook under any profile, with the same env every time."""
    home = tmp_path / "home"
    home.mkdir()

    def manifest(profile, name):
        path = tmp_path / f"{name}.json"
        path.write_text(dump_profile(profile))
        return path

    class World:
        pass

    w = World()
    w.home = home
    w.tmp = tmp_path
    w.agentwire = manifest(safety_ext.build_profile(), "agentwire")
    w.generic = manifest(GENERIC, "generic")
    w.manifest = manifest

    def run(kind, payload, profile, env=None):
        full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), **(env or {})}
        return subprocess.run(
            [sys.executable, "-I", "-m", "damage_control.hook", kind, "--profile", str(profile)],
            input=json.dumps(payload), capture_output=True, text=True, env=full_env, cwd=str(home), timeout=60)

    w.run = run
    w.bash = lambda command, profile, env=None: run("bash", {"tool_name": "Bash", "tool_input": {"command": command}},
                                                    profile, env)
    return w


def test_protected_paths_are_the_profiles(world):
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(world.home / ".agentwire" / "scheduler.yaml")}}
    got = (world.run("write", payload, world.agentwire).returncode, world.run("write", payload, world.generic).returncode)
    assert got == (2, 0), got


def test_protected_commands_are_the_profiles(world):
    got = (world.bash("agentwire tasks promote x", world.agentwire).returncode,
           world.bash("agentwire tasks promote x", world.generic).returncode)
    assert got == (2, 0), got
    migrate = (world.bash("agentwire safety migrate", world.agentwire).returncode,
               world.bash("agentwire safety migrate --dry-run", world.agentwire).returncode)
    assert migrate == (2, 0), "migrate is human-only; its dry run reads only"


def test_headless_env_and_default_grants_are_the_profiles(world):
    env = {"AGENTWIRE_UNATTENDED": "1", "DAMAGE_CONTROL_UNATTENDED": "1"}
    # git.commit is one of AgentWire's default grants; the engine ships none.
    got = (world.bash("git commit -m x", world.agentwire, env).returncode,
           world.bash("git commit -m x", world.generic, env).returncode)
    assert got == (0, 2), got
    # ...and an ungranted ask-tier command is refused, with the profile's own hint on how to get a grant.
    denied = world.bash("gh pr merge 1", world.agentwire, env)
    assert denied.returncode == 2 and ".agentwire.tasks.yml" in denied.stderr, denied.stderr


def test_the_audit_field_names_the_session_and_the_log_home(world):
    env = {"AGENTWIRE_SESSION_ID": "probe-session", "AGENTWIRE_AGENT_ID": "worker-9", "DAMAGE_CONTROL_SESSION_ID": "ignored"}
    world.bash("rm -rf /", world.agentwire, env)
    world.bash("rm -rf /", world.generic, env)

    rows = {}
    for label, root in (("agentwire", world.home / ".agentwire"), ("generic", world.home / ".damage-control")):
        files = list((root / "logs" / "damage-control").glob("*.jsonl"))
        assert files, f"{label}: no audit file under {root}"
        rows[label] = json.loads(files[0].read_text().splitlines()[-1])
    assert (rows["agentwire"]["session_id"], rows["agentwire"]["agent_id"]) == ("probe-session", "worker-9")
    assert (rows["generic"]["session_id"], rows["generic"]["agent_id"]) == ("unknown", "main")


def test_the_notifier_gets_the_event_as_json_on_stdin(world):
    sink = world.tmp / "event.json"
    notifier = (sys.executable, "-c", f"import sys; open({str(sink)!r}, 'w').write(sys.stdin.read())")
    manifest = world.manifest(safety_ext.build_profile()._replace(unattended_block_notifier=notifier), "notifying")

    blocked = world.bash("gh pr merge 1", manifest, {"AGENTWIRE_UNATTENDED": "1"})

    assert blocked.returncode == 2
    deadline = time.monotonic() + 10
    while not sink.exists() and time.monotonic() < deadline:  # the notifier is detached on purpose
        time.sleep(0.05)
    event = json.loads(sink.read_text())
    assert event["event"] == "unattended_block" and event["command"] == "gh pr merge 1", event
    assert event["rule_id"] == "tooldef.github-cli-merge-a-pull-request", event


def test_extra_rule_dirs_are_merged_after_the_generic_rules(world):
    rules = world.tmp / "extra-rules"
    rules.mkdir()
    (rules / "zz-extension.json").write_text(json.dumps({"bashToolPatterns": [
        {"id": "ext.xyzzy", "pattern": r"\bxyzzy-probe\b", "reason": "an extension rule"}]}))
    manifest = world.manifest(safety_ext.build_profile()._replace(
        extra_rules_dirs=(*safety_ext.build_profile().extra_rules_dirs, str(rules))), "extended")

    got = (world.bash("xyzzy-probe", manifest).returncode, world.bash("xyzzy-probe", world.agentwire).returncode)

    assert got == (2, 0), got
    # The generic rules are untouched by the extra directory.
    assert world.bash("rm -rf /", manifest).returncode == 2


def test_mcp_command_templates_make_the_outbound_tool_match_the_shell_rule(world):
    payload = {"tool_name": "mcp__agentwire__email_send",
               "tool_input": {"to": "a@example.com", "subject": "s", "body": "b"}}
    agentwire = world.run("mcp", payload, world.agentwire)
    generic = world.run("mcp", payload, world.generic)
    assert '"permissionDecision": "ask"' in agentwire.stdout and "agentwire email" in agentwire.stdout, agentwire.stdout
    assert generic.returncode == 0 and generic.stdout == "", generic.stdout


def test_the_config_home_decides_which_policy_file_is_protected_and_read(world):
    # The kill switch is the host's policy file. Under AgentWire's profile that is ~/.agentwire;
    # under the generic profile it is ~/.damage-control, so a stray ~/.agentwire policy does nothing.
    (world.home / ".agentwire").mkdir()
    (world.home / ".agentwire" / "damagecontrol.yml").write_text("enabled: false\n")
    got = (world.bash("rm -rf /", world.agentwire).returncode, world.bash("rm -rf /", world.generic).returncode)
    assert got == (0, 2), got


def test_default_packs_decide_what_an_undecided_install_loads(world):
    # AgentWire keeps the tmux pack on by default; the generic profile has no default packs.
    got = (world.bash("tmux kill-server", world.agentwire).returncode,
           world.bash("tmux kill-server", world.generic).returncode)
    assert got[0] != 0 and got[1] == 0, got


def test_the_profiles_notifier_argv_is_a_command_that_consumes_the_event(monkeypatch):
    """The profile's side of the notifier protocol: its argv parses to the handler that reads the stdin event."""
    from agentwire import safety_cli, safety_notify
    from agentwire.__main__ import build_parser

    argv = safety_ext.build_profile().unattended_block_notifier
    assert argv[0] == "agentwire"
    args = build_parser().parse_args(list(argv[1:]))
    assert args.func is safety_cli.cmd_safety_notify_unattended_block

    spooled = {}
    monkeypatch.setattr(safety_notify, "record_block", lambda **kw: spooled.update(kw) or {"spooled": True})
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(
        {"event": "unattended_block", "reason": "why", "rule_id": "git.push", "command": "git push"})))

    assert args.func(args) == 0
    assert (spooled["reason"], spooled["rule_id"], spooled["command"]) == ("why", "git.push", "git push")
