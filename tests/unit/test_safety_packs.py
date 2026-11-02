"""Rule groups and opt-in packs: runtime state, migration, human-only gate (#1141, #1209 M4).

The hooks read ``packs:`` / ``disabled_groups:`` from the policy file on every call, so
enabling or disabling takes effect on the next tool call and touches no file on disk.
"""

import shutil
from types import SimpleNamespace

import pytest
from damage_control import engine, packs
from damage_control.engine import check_protected_bash_command
from damage_control.groups import default_packs, registry

from agentwire import safety_commands, safety_ext, safety_migrate, safety_packs_cli
from tests.unit.test_safety_install import fake_env  # noqa: F401  (fixture)

# One destructive command per pack: with the pack's files installed each is
# not "allow"; without them it is.
PROBES = {
    "aws": "aws s3 rb s3://bucket --force",
    "databases": "redis-cli FLUSHALL",
    "containers": "docker system prune -af",
    "infrastructure": "terraform destroy -auto-approve",
}

# Destructive generic ops that used to live in a pack: a fresh install (every
# pack off) must still refuse them.
DEFAULT_PROTECTED = ["gh repo delete o/r", "npm unpublish left-pad"]


@pytest.fixture
def home(fake_env, monkeypatch):  # noqa: F811
    """An isolated ~/.agentwire that install treats as canonical, with a human at the terminal."""
    monkeypatch.setattr("agentwire.tasks_cli._host_context_ok", lambda: True)
    return fake_env


def overlay(section):
    d = safety_commands.RULES_DIR if section == "rules" else safety_commands.TOOLDEFS_DIR
    return {p.name for p in d.glob("*.yaml")} if d.is_dir() else set()


def decisions():
    return {k: safety_commands.check_command_safety(c)["decision"] for k, c in PROBES.items()}


def install_old_layout():
    """What every pre-package machine looks like: ALL bundled files copied into the overlay, no `packs:` key."""
    sources = {
        safety_commands.RULES_DIR: [p for d in engine.bundled_rule_sources() for p in d.glob("*.yaml")],
        safety_commands.TOOLDEFS_DIR: list(engine.bundled_tooldefs_dir().glob("*.yaml")),
    }
    for target, files in sources.items():
        target.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy(path, target / path.name)
    safety_commands.DAMAGECONTROL_FILE.write_text("enabled: true\n")


def test_fresh_install_enables_the_default_packs_and_nothing_else(home):
    safety_ext.install(log=lambda *_: None)
    assert packs.effective_state(safety_commands.DAMAGECONTROL_FILE)["packs"] == sorted(default_packs())
    assert overlay("rules") == set() and overlay("tooldefs") == set()  # state lives in the policy file, not on disk
    assert set(decisions().values()) == {"allow"}
    weak = [c for c in DEFAULT_PROTECTED
            if safety_commands.check_command_safety(c)["decision"] != "block"]
    assert not weak, weak


def test_upgrade_of_a_pre_package_install_changes_no_decision(home):
    install_old_layout()
    before = decisions()
    assert set(before.values()) != {"allow"}, "probes must bite with the pack files present"
    plan = safety_migrate.classify()
    assert plan["rules"]["pristine"] and not plan["rules"]["edited"] and not plan["tooldefs"]["edited"]
    assert safety_migrate.migrate(log=lambda *_: None) == 0
    assert decisions() == before
    state = packs.read_state(safety_commands.DAMAGECONTROL_FILE)
    assert set(state["packs"]) == {n for n, e in registry().items() if e.kind == "pack"}
    assert overlay("rules") == set() and overlay("tooldefs") == set(), "pristine copies must not shadow the bundle"


def test_enable_and_disable_take_effect_on_the_next_call_and_write_no_rule_file(home):
    safety_ext.install(log=lambda *_: None)
    assert decisions()["aws"] == "allow"
    assert safety_packs_cli.cmd_packs_enable(SimpleNamespace(name="aws")) == 0
    assert decisions()["aws"] != "allow"
    assert safety_packs_cli.cmd_packs_disable(SimpleNamespace(name="aws")) == 0
    assert decisions()["aws"] == "allow"
    assert overlay("rules") == set() and overlay("tooldefs") == set()


def test_a_hand_edited_file_of_a_disabled_pack_still_loads(home):
    """Disabling a pack switches off the BUNDLED file; a file the human wrote is theirs and stays on."""
    safety_ext.install(log=lambda *_: None)
    safety_commands.RULES_DIR.mkdir(parents=True, exist_ok=True)
    (safety_commands.RULES_DIR / "aws.yaml").write_text(
        "bashToolPatterns:\n  - id: mine.aws-bucket-removal\n    pattern: '\\baws\\s+s3\\s+rb\\b'\n"
        "    reason: my own aws rule\n")
    assert decisions()["aws"] == "block"
    assert safety_migrate.classify()["rules"]["edited"] == ["aws.yaml"]


def test_group_opt_out_and_kernel_refusal(home):
    safety_ext.install(log=lambda *_: None)
    probe = "agentwire email --to someone@example.com"
    default = safety_commands.check_command_safety(probe)["decision"]
    assert default != "allow", "the outbound group must bite by default"
    assert safety_packs_cli.cmd_groups_disable(SimpleNamespace(name="outbound")) == 0
    assert safety_commands.check_command_safety(probe)["decision"] == "allow"
    assert safety_packs_cli.cmd_groups_enable(SimpleNamespace(name="outbound")) == 0
    assert safety_commands.check_command_safety(probe)["decision"] == default
    refused = [n for n, e in registry().items() if e.kernel
               and safety_packs_cli.cmd_groups_disable(SimpleNamespace(name=n)) == 0]
    assert not refused, refused
    assert safety_commands.check_command_safety("rm -rf /")["decision"] == "block"


def test_mutations_refuse_without_a_human_and_leave_state_alone(home, monkeypatch):
    safety_ext.install(log=lambda *_: None)
    monkeypatch.setattr("agentwire.tasks_cli._host_context_ok", lambda: False)
    before = safety_commands.DAMAGECONTROL_FILE.exists()
    codes = [
        safety_packs_cli.cmd_packs_enable(SimpleNamespace(name="aws")),
        safety_packs_cli.cmd_packs_disable(SimpleNamespace(name="aws")),
        safety_packs_cli.cmd_groups_disable(SimpleNamespace(name="outbound")),
    ]
    assert codes == [1, 1, 1]
    assert safety_commands.DAMAGECONTROL_FILE.exists() == before, "a refused mutation must not create the policy file"


def test_undecided_policy_loads_only_the_hosts_default_packs(home):
    """No `packs:` key = never decided: default groups plus the host's default packs, no vendor pack (#1209 M5)."""
    safety_commands.DAMAGECONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    safety_commands.DAMAGECONTROL_FILE.write_text("enabled: true\n")
    assert engine.load_safety_config()["packs"] is None
    assert decisions()["aws"] == "allow" and decisions()["databases"] == "allow"
    assert safety_commands.check_command_safety("tmux kill-server")["decision"] != "allow"  # AgentWire's default pack


def test_protected_pattern_blocks_mutation_not_reads():
    blocked = ["agentwire safety packs enable aws", "agentwire  safety groups disable outbound",
               "cd /x && agentwire safety packs disable aws"]
    allowed = ["agentwire safety packs list", "agentwire safety packs status --json",
               'echo "agentwire safety packs enable aws"']
    wrong = [c for c in blocked if not check_protected_bash_command(c)[0]]
    wrong += [c for c in allowed if check_protected_bash_command(c)[0]]
    assert not wrong, wrong
