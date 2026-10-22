"""#1152 — `delete-recursive`: a user-owned opt-in for `rm -rf <regenerable dir>`.

The default stays hard-blocked. A human writes `allow: [delete-recursive]` in a
PROTECTED policy file; the removal is then allowed only when EVERY operand is
covered, and never for the things no entry can reach (home, repo root, cwd,
`.git`, `..`, symlinks out, unresolvable operands). Every table collects the
offenders and asserts none, so one failure lists them all.
"""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from tests.conftest import HOOKS_DIR

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"
RANK = {"allow": 0, "ask": 1, "block": 2}
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


def test_basename_class_entry_in_the_global_file(world):
    entries = [{"path": "*/node_modules/*", "allow": DR}, {"path": "*/dist/*", "allow": DR}]
    allowed = ["rm -rf node_modules", "rm -rf packages/a/node_modules", "rm -rf dist"]
    blocked = ["rm -rf src", "rm -rf packages", "rm -rf packages/a", "rm -rf node_modules src"]
    bad = [c for c in allowed if world.decide(c, global_=entries)["decision"] != "allow"]
    bad += [c for c in blocked if world.decide(c, global_=entries)["decision"] != "block"]
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


def test_cwd_relative_operands_inside_the_covered_dir_stay_blocked(world):
    entries = _entries("dist")
    inside = str(world.repo / "dist")
    commands = ["rm -rf .", "rm -rf ./*", "rm -rf *", "rm -rf ../dist", f"rm -rf {inside}", "rm -rf ../dist/x"]
    bad = [c for c in commands if world.decide(c, project=entries, where=inside)["decision"] == "allow"]
    assert not bad, bad
    bad = [c for c in ("cd dist && rm -rf .", "cd dist && rm -rf ./*", "cd dist && rm -rf *")
           if world.decide(c, project=entries)["decision"] == "allow"]
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


def test_all_does_not_imply_delete_recursive(world):
    bad = []
    for allow in ("all", ["all"], ["delete", "move", "write", "edit", "read", "chmod"]):
        entries = [{"path": "dist/*", "allow": allow}, {"path": "*/dist/*", "allow": allow}]
        if world.decide("rm -rf dist", project=entries)["decision"] == "allow":
            bad.append(allow)
    assert not bad, bad
    # existing behaviour of an `all` entry is untouched: plain rm of a file still allowed
    (world.repo / "dist" / "a.js").write_text("x")
    assert world.decide(f"rm {world.repo}/dist/a.js", project=[{"path": "*/dist/*", "allow": "all"}])["decision"] == "allow"
    # and a bare-string operation works like the list form
    assert world.decide("rm -rf dist", project=[{"path": "dist/*", "allow": "delete-recursive"}])["decision"] == "allow"


def test_the_entry_must_carry_the_operation_and_be_covered_by_it(world):
    entries = _entries("node_modules")
    assert world.decide("rm -rf dist", project=entries)["decision"] == "block"
    assert world.decide("rm -rf dist")["decision"] == "block"
    assert world.decide("rm -rf node_modules", project=_entries("node_modules", allow=["delete"]))["decision"] == "block"


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


def test_good_shapes_pass_the_entry_check(bash_hook):
    problem = bash_hook.delete_recursive_entry_problem
    bad = [p for p in ("*/node_modules/*", "*/dist/*", "*/.next/*", "*/__pycache__/*", "/abs/proj/dist/*", "/abs/proj/dist", "~/work/app/build/*")
           if problem(p) is not None]
    bad += [p for p in ("*", "/*", "~/*", "*/*", "~", "/", "*/.git/*", "dist/*", "")
            if problem(p) is None]
    assert not bad, bad
    assert problem("/r/p/dist/*", project_root="/r/p") is None
    assert problem("/r/p/*", project_root="/r/p") is not None


def test_policy_only_comes_from_the_protected_files(world):
    entries = _entries("dist")
    assert world.decide("rm -rf dist", agentwire_yml=entries)["decision"] == "block"
    assert world.decide("rm -rf dist", project=entries)["decision"] == "allow"


def test_a_wrapped_command_is_never_more_permitted_than_the_bare_one(world):
    entries = _entries("dist")
    bare = world.decide("rm -rf dist", project=entries)["decision"]
    assert bare == "allow"
    wrappers = [
        "sudo rm -rf dist",
        "env rm -rf dist",
        "env A=1 rm -rf dist",
        "timeout 5 rm -rf dist",
        "nice rm -rf dist",
        "command rm -rf dist",
        "sh -c 'rm -rf dist'",
        'bash -c "rm -rf dist"',
        "ssh host 'rm -rf dist'",
        "xargs rm -rf <<< dist",
        "echo dist | xargs rm -rf",
        "if true; then rm -rf dist; fi",
        "for d in dist; do rm -rf $d; done",
        "( rm -rf dist )",
        "{ rm -rf dist; }",
        "time rm -rf dist",
        "nohup rm -rf dist",
    ]
    bad = [w for w in wrappers if RANK[world.decide(w, project=entries)["decision"]] < RANK[bare]]
    assert not bad, bad
    # a wrapper around an UNCOVERED target is exactly as blocked as the bare form
    assert world.decide("sudo rm -rf src", project=entries)["decision"] == "block"
    # compound: a covered rm cannot launder an uncovered one
    assert world.decide("rm -rf dist && rm -rf src", project=entries)["decision"] == "block"
    assert world.decide("rm -rf dist; rm -rf /etc", project=entries)["decision"] == "block"
    assert world.decide("rm -rf dist && echo ok", project=entries)["decision"] == "allow"


def test_the_allowed_row_names_the_entry(world):
    entries = _entries("dist")
    verdict = world.decide("rm -rf dist", project=entries)
    assert verdict["decision"] == "allow"
    assert str(world.repo / "dist" / "*") in verdict["allowed_by"] and "delete-recursive" in verdict["allowed_by"]
    assert "allowed_by" not in world.decide("echo hi", project=entries)


def test_no_entries_means_no_change(world):
    commands = ["rm -rf dist", "rm -rf node_modules .next", "rm -rf ./dist/", "rm -rf /", "rm -R dist", "rm --recursive dist"]
    bad = [c for c in commands if world.decide(c)["decision"] != "block" or "allowed_by" in world.decide(c)]
    assert not bad, bad


def test_the_audit_row_carries_allowed_by(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("audit_logger_dr", HOOKS_DIR / "audit_logger.py")
    audit_logger = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit_logger)
    monkeypatch.setenv("AGENTWIRE_DIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    audit_logger.HOOK_INPUT = {"session_id": "c", "tool_use_id": "t1"}
    audit_logger.log_allowed("Bash", "rm -rf dist", allowed_by="delete-recursive: /r/dist/*")
    audit_logger.log_allowed("Bash", "ls")
    rows = [json.loads(line) for f in (tmp_path / "logs" / "damage-control").glob("*.jsonl") for line in f.read_text().splitlines()]
    assert [r.get("allowed_by") for r in rows] == ["delete-recursive: /r/dist/*", None]



def test_a_project_reached_through_a_symlinked_path_still_works(world, tmp_path):
    """macOS `/tmp` -> `/private/tmp`: cwd is the symlink spelling, the covered root is anchored in real form."""
    alias = tmp_path / "alias"
    alias.symlink_to(world.repo)
    bad = [c for c in ("rm -rf dist", "rm -rf ./dist/", "rm -rf node_modules")
           if world.decide(c, project=_entries("dist", "node_modules"), where=str(alias))["decision"] != "allow"]
    assert not bad, bad
    assert world.decide("rm -rf src", project=_entries("dist"), where=str(alias))["decision"] == "block"


def _audit_row(command, cwd, decision="blocked", tool="Bash"):
    return {"timestamp": "2026-10-20T10:00:00", "tool": tool, "command": command, "decision": decision, "cwd": cwd}


def test_suggest_lists_recurring_regenerable_targets_and_prints_the_snippet(tmp_path):
    from agentwire.safety.suggest import format_suggestions, suggest_delete_recursive

    proj_a, proj_b = tmp_path / "a", tmp_path / "b"
    for p in (proj_a, proj_b):
        (p / ".git").mkdir(parents=True)
    rows = (
        [_audit_row("rm -rf dist", str(proj_a))] * 3
        + [_audit_row("rm -rf node_modules .next", str(proj_a))] * 2
        + [_audit_row("rm -rf dist", str(proj_b))]            # once: below --min
        + [_audit_row("rm -rf src", str(proj_a))] * 5          # not regenerable-looking
        + [_audit_row("rm -rf dist", str(proj_a), decision="allowed")] * 4
        + [_audit_row("rm -rf $X", str(proj_a))] * 4            # unresolvable
        + [_audit_row("rm -rf dist", str(proj_a), tool="Edit")] * 4
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "2999-01-01.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    result = suggest_delete_recursive(days=100000, min_count=2, log_dir=log_dir)
    (only,) = result["suggestions"]
    assert only["project"] == str(proj_a) and only["targets"] == {"dist": 3, "node_modules": 2, ".next": 2}
    assert yaml.safe_load(only["snippet"]) == {
        "allowed_paths": [{"path": f"{n}/*", "allow": ["delete-recursive"]} for n in ("dist", "node_modules", ".next")]
    }
    assert "never writes policy" in format_suggestions(result)
    assert not (proj_a / ".damagecontrol.yml").exists()


def test_the_suggested_snippet_is_accepted_and_allows_what_it_was_derived_from(world):
    from agentwire.safety.suggest import snippet_for

    entries = yaml.safe_load(snippet_for(["dist", "node_modules"]))["allowed_paths"]
    assert world.decide("rm -rf dist node_modules", project=entries)["decision"] == "allow"


def test_doctor_lists_active_entries_and_names_a_refused_one(world, bash_hook, bundled_config, capsys):
    from agentwire.doctor_cli import _report_delete_recursive

    world.decide("true", project=[
        {"path": "dist/*", "allow": DR},
        {"path": "*", "allow": DR},
        {"path": "node_modules/*", "allow": "all"},
    ])
    issues = _report_delete_recursive(bash_hook.load_allowed_paths(bundled_config))
    out = capsys.readouterr().out
    assert issues == 1
    assert "active" in out and "dist" in out and "REFUSED" in out and "'" + str(world.repo / "*") + "'" in out
    assert str(world.repo / "node_modules") not in out  # an `all` entry is not a delete-recursive entry
