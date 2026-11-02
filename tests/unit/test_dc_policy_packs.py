"""Pack/group state is read from the protected policy at RUN time; undecided means packs OFF (#1209 M5).

M4 loaded every pack for a machine whose policy had no ``packs:`` key. The invariant now:
default groups on, kernel groups always on, and only the packs the policy (or, undecided, the
host's defaults) names. A machine that has decided its packs must see exactly what it saw before.
"""

import pytest
from damage_control import engine
from damage_control.groups import registry
from damage_control.profile import GENERIC

PACKS = sorted(e.name for e in registry().values() if e.kind == "pack")
PACK_SOURCES = {stem for e in registry().values() if e.kind == "pack"
                for stem in (f.rsplit(".", 1)[0] for f in e.files("rules") + e.files("tooldefs"))}


@pytest.fixture
def policy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".damage-control").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DAMAGE_CONTROL_HOME", raising=False)
    monkeypatch.setattr(engine, "PROFILE", GENERIC)
    monkeypatch.chdir(tmp_path)
    return home / ".damage-control" / "damagecontrol.yml"


def sources():
    return {r["source"] for r in engine.load_effective_config()["bashToolPatterns"]}


def ids():
    return {r["id"] for r in engine.load_effective_config()["bashToolPatterns"]}


def test_an_undecided_install_loads_the_default_groups_and_no_pack(policy):
    loaded = sources()
    assert loaded, "the default groups must load"
    assert not loaded & PACK_SOURCES, sorted(loaded & PACK_SOURCES)
    assert {"core", "git"} <= loaded


def test_a_decided_install_loads_exactly_the_packs_it_lists(policy):
    bad = []
    for pack in PACKS:
        policy.write_text(f"packs:\n  - {pack}\n")
        entry = registry()[pack]
        want = {f.rsplit(".", 1)[0] for f in entry.files("rules") + entry.files("tooldefs")}
        got = sources() & PACK_SOURCES
        # a pack's files may hold no bash pattern (tooldef-only or path-only); never MORE than it owns
        if not got <= want:
            bad.append((pack, sorted(got - want)))
    assert not bad, bad


def test_a_machine_that_decided_every_pack_enforces_the_full_ruleset(policy):
    """The owner's shape: packs decided. Must equal the unfiltered union of every bundled rule."""
    policy.write_text("packs:\n" + "".join(f"  - {p}\n" for p in PACKS))
    full = engine.load_config(engine.bundled_rule_sources(), engine.bundled_tooldefs_dir())
    assert ids() == {r["id"] for r in full["bashToolPatterns"]}


def test_an_unreadable_policy_means_groups_on_packs_off_never_everything_off(policy):
    bad = []
    for name, text in {"yaml-error": "packs: [unclosed\n  - : :\n", "not-a-mapping": "- just\n- a list\n"}.items():
        policy.write_text(text)
        loaded = sources()
        if not {"core", "git"} <= loaded or loaded & PACK_SOURCES:
            bad.append((name, sorted(loaded & PACK_SOURCES)))
    assert not bad, bad


def test_a_kernel_group_cannot_be_disabled_and_other_groups_can(policy):
    policy.write_text("packs: []\ndisabled_groups:\n  - core\n  - git\n")
    loaded = sources()
    assert "core" in loaded and "git" not in loaded


def test_the_hosts_default_packs_load_when_undecided(policy, monkeypatch):
    monkeypatch.setattr(engine, "PROFILE", GENERIC._replace(default_packs=("tmux",)))
    assert "tmux" in sources()
    policy.write_text("packs: []\n")  # a decision overrides the host's defaults
    assert "tmux" not in sources()
