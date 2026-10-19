"""Rule groups and opt-in packs: state, heal, migration, human-only gate (#1141)."""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentwire import safety_commands, safety_packs_cli
from agentwire.safety import packs
from agentwire.safety.groups import REGISTRY, bundled_files
from agentwire.safety._core import check_protected_bash_command

PKG = Path(safety_commands.__file__).parent

# One destructive command per pack: with the pack's files installed each is
# not "allow"; without them it is.
PROBES = {
    "aws": "aws s3 rb s3://bucket --force",
    "databases": "redis-cli FLUSHALL",
    "containers": "docker system prune -af",
    "infrastructure": "terraform destroy -auto-approve",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    from agentwire.safety import provenance as _prov
    monkeypatch.setattr(_prov, "canonical_package_dir", lambda: PKG.resolve())
    cfg = home / ".agentwire"
    monkeypatch.setattr(safety_commands, "CONFIG_DIR", cfg)
    monkeypatch.setattr(safety_commands, "HOOKS_DIR", cfg / "hooks" / "damage-control")
    monkeypatch.setattr(safety_commands, "LOGS_DIR", cfg / "logs" / "damage-control")
    monkeypatch.setattr(safety_commands, "RULES_DIR", cfg / "damage-control")
    monkeypatch.setattr(safety_commands, "TOOLDEFS_DIR", cfg / "tooldefs")
    monkeypatch.setattr(safety_commands, "DAMAGECONTROL_FILE", cfg / "damagecontrol.yml")
    # A human at a terminal, unless a test says otherwise.
    monkeypatch.setattr("agentwire.tasks_cli._host_context_ok", lambda: True)
    return home


def installed(section):
    d = safety_commands.RULES_DIR if section == "rules" else safety_commands.TOOLDEFS_DIR
    return {p.name for p in d.glob("*.yaml")}


def decisions():
    return {k: safety_commands.check_command_safety(c)["decision"] for k, c in PROBES.items()}


def pack_files(name):
    e = REGISTRY[name]
    return set(e.rules), set(e.tooldefs)


def install_old_layout():
    """What every pre-packs machine looks like: ALL bundled files, no `packs:` key."""
    src = bundled_files(PKG)
    for section, d, sdir in (
        ("rules", safety_commands.RULES_DIR, PKG / "hooks" / "damage-control" / "rules"),
        ("tooldefs", safety_commands.TOOLDEFS_DIR, PKG / "tooldefs"),
    ):
        d.mkdir(parents=True, exist_ok=True)
        for name in src[section]:
            shutil.copy(sdir / name, d / name)
    safety_commands.DAMAGECONTROL_FILE.write_text("enabled: true\n")


def test_fresh_install_has_groups_not_packs(home):
    safety_commands.heal_damage_control(quiet=True)
    wrong = []
    for section in ("rules", "tooldefs"):
        for e in REGISTRY.values():
            have = set(getattr(e, section)) & installed(section)
            if e.kind == "pack" and have:
                wrong.append(f"pack {e.name} installed {sorted(have)}")
            if e.kind == "group" and set(getattr(e, section)) - installed(section):
                wrong.append(f"group {e.name} missing files")
    assert not wrong, wrong
    assert packs.read_state(safety_commands.DAMAGECONTROL_FILE)["packs"] == []
    assert set(decisions().values()) == {"allow"}


def test_upgrade_of_old_install_changes_no_decision(home):
    install_old_layout()
    before = decisions()
    assert set(before.values()) != {"allow"}, "probes must bite with the pack files present"
    summary = safety_commands.heal_damage_control(quiet=True)
    assert decisions() == before
    assert not summary["rules_removed"] and not summary["tooldefs_removed"]
    state = packs.read_state(safety_commands.DAMAGECONTROL_FILE)
    assert set(state["packs"]) == {n for n, e in REGISTRY.items() if e.kind == "pack"}
    for section in ("rules", "tooldefs"):
        assert installed(section) == bundled_files(PKG)[section]


def test_migration_counts_an_edited_pack_file_as_in_use(home):
    install_old_layout()
    aws = safety_commands.RULES_DIR / "aws.yaml"
    aws.write_text(aws.read_text() + "\n# mine\n")
    safety_commands.heal_damage_control(quiet=True)
    assert "aws" in packs.read_state(safety_commands.DAMAGECONTROL_FILE)["packs"]


def test_enable_installs_and_disable_removes_pristine(home):
    safety_commands.heal_damage_control(quiet=True)
    assert safety_packs_cli.cmd_packs_enable(SimpleNamespace(name="aws")) == 0
    rules, tools = pack_files("aws")
    assert rules <= installed("rules") and tools <= installed("tooldefs")
    assert decisions()["aws"] != "allow"
    assert safety_packs_cli.cmd_packs_disable(SimpleNamespace(name="aws")) == 0
    assert not rules & installed("rules") and not tools & installed("tooldefs")
    assert decisions()["aws"] == "allow"


def test_disable_never_removes_an_edited_file(home):
    safety_commands.heal_damage_control(quiet=True)
    safety_packs_cli.cmd_packs_enable(SimpleNamespace(name="aws"))
    f = safety_commands.RULES_DIR / "aws.yaml"
    f.write_text(f.read_text() + "\n# hand edit\n")
    safety_packs_cli.cmd_packs_disable(SimpleNamespace(name="aws"))
    assert f.exists() and "# hand edit" in f.read_text()
    assert safety_commands.heal_damage_control(quiet=True)["rules_kept_edited"] == ["aws.yaml"]


def test_disabled_pack_is_not_reported_as_drift(home):
    safety_commands.heal_damage_control(quiet=True)
    drift = {**safety_commands.rules_drift(), **safety_commands.tooldefs_drift()}
    bad = {f: s for f, s in drift.items() if s != "ok"}
    assert not bad, bad


def test_group_opt_out_and_kernel_refusal(home):
    safety_commands.heal_damage_control(quiet=True)
    assert safety_packs_cli.cmd_groups_disable(SimpleNamespace(name="outbound")) == 0
    assert "outbound.yaml" not in installed("rules")
    assert safety_packs_cli.cmd_groups_enable(SimpleNamespace(name="outbound")) == 0
    assert "outbound.yaml" in installed("rules")
    refused = [n for n, e in REGISTRY.items() if e.kernel
               and safety_packs_cli.cmd_groups_disable(SimpleNamespace(name=n)) == 0]
    assert not refused, refused
    assert {"core.yaml", "control-plane.yaml"} <= installed("rules")


def test_mutations_refuse_without_a_human_and_leave_state_alone(home, monkeypatch):
    safety_commands.heal_damage_control(quiet=True)
    monkeypatch.setattr("agentwire.tasks_cli._host_context_ok", lambda: False)
    before = safety_commands.DAMAGECONTROL_FILE.read_text()
    codes = [
        safety_packs_cli.cmd_packs_enable(SimpleNamespace(name="aws")),
        safety_packs_cli.cmd_packs_disable(SimpleNamespace(name="aws")),
        safety_packs_cli.cmd_groups_disable(SimpleNamespace(name="outbound")),
    ]
    assert codes == [1, 1, 1]
    assert safety_commands.DAMAGECONTROL_FILE.read_text() == before


def test_protected_pattern_blocks_mutation_not_reads():
    blocked = ["agentwire safety packs enable aws", "agentwire  safety groups disable outbound",
               "cd /x && agentwire safety packs disable aws"]
    allowed = ["agentwire safety packs list", "agentwire safety packs status --json",
               'echo "agentwire safety packs enable aws"']
    wrong = [c for c in blocked if not check_protected_bash_command(c)[0]]
    wrong += [c for c in allowed if check_protected_bash_command(c)[0]]
    assert not wrong, wrong


def test_write_state_preserves_comments_and_other_keys(tmp_path):
    f = tmp_path / "damagecontrol.yml"
    f.write_text("# keep me\nenabled: true\ndisabled_rules:\n  - git.push\npacks:\n  - aws\n"
                 "allowed_paths: []\n")
    packs.write_state(f, packs=["aws", "gcp"], disabled_groups=["remote"])
    text = f.read_text()
    assert "# keep me" in text and "- git.push" in text and "allowed_paths: []" in text
    state = packs.read_state(f)
    assert state == {"packs": ["aws", "gcp"], "disabled_groups": ["remote"]}


def test_overview_suggests_only_when_vendor_cli_present(home, monkeypatch):
    monkeypatch.setattr(safety_packs_cli.shutil, "which", lambda b: "/bin/x" if b == "docker" else None)
    rows = {r["name"]: r for r in safety_packs_cli.overview()}
    suggested = sorted(n for n, r in rows.items() if r["suggest_enable"])
    assert suggested == ["containers"]
