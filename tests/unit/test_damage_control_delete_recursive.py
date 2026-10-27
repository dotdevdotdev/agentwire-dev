"""#1152 — `delete-recursive`: a user-owned opt-in for `rm -rf <regenerable dir>`.

The default stays hard-blocked. A human writes `allow: [delete-recursive]` in a
PROTECTED policy file; the removal is then allowed only when EVERY operand is
covered, and never for the things no entry can reach (home, repo root, cwd,
`.git`, `..`, symlinks out, unresolvable operands). Every table collects the
offenders and asserts none, so one failure lists them all.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"
DR = ["delete-recursive"]


@pytest.fixture(scope="module")
def bundled_config(bash_hook):
    cfg = bash_hook.load_config(RULES_DIR, TOOLDEFS_DIR)
    assert not cfg.get("_parser_unavailable"), "rules failed to load"
    return cfg


@pytest.fixture
def world(bash_hook, bundled_config, monkeypatch, tmp_path):
    """A git repo under a fake HOME; ``decide`` writes policy files and judges a command."""
    home = tmp_path / "home"
    (home / ".agentwire").mkdir(parents=True)
    repo = tmp_path / "proj"
    for d in (".git", "dist", "node_modules", ".next", "src", "packages/a/node_modules"):
        (repo / d).mkdir(parents=True)
    outside = tmp_path / "outside"
    for d in ("dist", "build", "x"):
        (outside / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PWD", str(repo))
    monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)
    monkeypatch.chdir(repo)

    class World:
        pass

    w = World()
    w.home, w.repo, w.outside = home, repo, outside

    def decide(command, project=None, global_=None, where=None, agentwire_yml=None):
        pf = repo / ".damagecontrol.yml"
        gf = home / ".agentwire" / "damagecontrol.yml"
        for f, entries in ((pf, project), (gf, global_)):
            if entries is None:
                if f.exists():
                    f.unlink()
            else:
                f.write_text(yaml.safe_dump({"allowed_paths": entries}))
        if agentwire_yml is not None:
            (repo / ".agentwire.yml").write_text(yaml.safe_dump({"allowed_paths": agentwire_yml}))
        cfg = dict(bundled_config, safety={"enabled": True, "disabled_rules": [], "unattended_allow": []})
        monkeypatch.setenv("PWD", where or str(repo))
        return bash_hook.check_command(command, cfg, cwd=where or str(repo))

    w.decide = decide
    return w


def _entries(*names, allow=DR):
    return [{"path": f"{n}/*", "allow": allow} for n in names]


def test_covered_removals_are_allowed(world):
    entries = _entries("dist", "node_modules", ".next")
    commands = [
        "rm -rf dist",
        "rm -rf node_modules .next",
        "rm -rf ./dist/",
        f"rm -rf {world.repo}/dist",
        "rm -R dist",
        "rm -fr dist",
        "rm -r -f dist",
        "rm --recursive --force dist",
        "rm -rf dist/sub/deep",
        f"cd {world.repo} && rm -rf dist",
    ]
    bad = [c for c in commands if world.decide(c, project=entries)["decision"] != "allow"]
    assert not bad, bad


def test_uncovered_or_unresolvable_operands_stay_blocked(world):
    entries = _entries("dist", "node_modules")
    commands = [
        "rm -rf dist /etc",
        "rm -rf dist src",
        "rm -rf src",
        "rm -rf dist/..",
        "rm -rf dist/../src",
        "rm -rf ../proj/dist",
        "rm -rf $UNSET_VAR",
        "rm -rf $HOME",
        "rm -rf dist/*",
        "rm -rf d*",
        "rm -rf dist{,x}",
        "rm -rf $(echo dist)",
        "rm -rf `echo dist`",
        "rm -rf /",
        "rm -rf ~",
        "rm -rf .",
        "rm -rf ./",
        "rm -rf ..",
        f"rm -rf {world.repo}",
        f"rm -rf {world.repo}/",
        f"rm -rf {world.repo.parent}",
        "rm -rf .git",
        "rm -rf dist/.git",
        "rm -rf ~/.agentwire",
        "rm -rf ~/.claude",
        "rm -rf ~/.agentwire/damagecontrol.yml",
        "rm -rf .damagecontrol.yml",
        "rm dist -rf",
    ]
    bad = [c for c in commands if world.decide(c, project=entries)["decision"] == "allow"]
    assert not bad, bad


def test_symlinks_out_of_the_covered_tree_stay_blocked(world):
    (world.repo / "link").symlink_to(world.outside)
    (world.repo / "build").symlink_to(world.outside / "build")
    entries = _entries("dist", "build") + [{"path": "*/dist/*", "allow": DR}, {"path": "*/build/*", "allow": DR}]
    commands = [
        "rm -rf link/dist",       # a symlink on the way in: real target is outside
        "rm -rf link/build",
        "rm -rf build",           # the covered root IS a symlink
        "rm -rf build/",
        "rm -rf build/x",         # through the covered-root symlink
        f"rm -rf {world.repo}/link/dist",
    ]
    bad = [c for c in commands if world.decide(c, project=entries)["decision"] == "allow"]
    assert not bad, bad
    assert (world.outside / "dist").exists()


BROAD = ["*", "**", "/*", "~/*", "*/*", "*/**", "{repo}/*", "{repo}", "*/.git/*", ".git/*", "/", "~", "src/*/dist/*", "{parent}/*", "dist/../*"]


def test_broad_entries_are_refused_at_load_and_do_nothing(world, bash_hook, bundled_config):
    bad = []
    for path in BROAD:
        path = path.format(repo=world.repo, parent=world.repo.parent)
        entries = [{"path": path, "allow": DR}]
        if world.decide("rm -rf dist", project=entries)["decision"] == "allow":
            bad.append(("allowed", path))
        if world.decide("rm -rf dist", global_=entries)["decision"] == "allow":
            bad.append(("allowed-global", path))
        world.decide("true", project=entries)
        loaded = [e for e in bash_hook.load_allowed_paths(bundled_config) if e["path"].rstrip("/*") or True]
        flagged = [e for e in loaded if DR[0] in e["allow"] and not e.get("dr_refused")]
        if flagged:
            bad.append(("not-refused", path))
    assert not bad, bad


