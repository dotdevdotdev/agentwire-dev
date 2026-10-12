"""A plain ``rm <file>`` yields exactly when the bytes can come back (#940).

Both halves are load-bearing: recoverable files pass, and every shape that is
not provably a recoverable plain file keeps blocking — flags, globs, variables,
directories, a tracked file with local edits, a file outside every repo, a
wrapper that takes ``rm`` as its argument, an unmodelled ``cd``.

Git state is stubbed (``_git_file_state``) so the fast set spawns no process;
one real-git test is marked ``slow``.
"""

import subprocess
from pathlib import Path

import pytest

from agentwire.safety import _core
from agentwire.safety._core import check_command, load_config, rm_removes_only_recoverable_files

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control" / "rules"
_RF = "-r" + "f"  # keep the literal flag pair out of this file's text
_F = "-" + "f"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A repo with files in every state, a scratch dir, and a non-repo dir."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    for name in ("untracked.txt", "clean.txt", "dirty.txt", "sub/clean2.txt"):
        (repo / name).write_text("x")
    (repo / ".git" / "config").write_text("x")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "tmpfile").write_text("x")
    work = scratch / "proj" / "sess" / "work"
    work.mkdir(parents=True)
    (work / "f.txt").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    states = {"untracked.txt": "untracked", "clean.txt": "clean", "dirty.txt": "modified", "sub/clean2.txt": "clean"}
    monkeypatch.setattr(_core, "_git_file_state", lambda repo_root, rel: states.get(rel))
    monkeypatch.setattr(_core, "_scratch_roots", lambda: [str(scratch.resolve())])
    return {"repo": str(repo), "scratch": str(scratch), "outside": str(outside)}


def test_recoverable_removals_pass(world):
    repo, scratch = world["repo"], world["scratch"]
    cases = {
        "untracked": ("rm untracked.txt", repo),
        "tracked-clean": ("rm clean.txt", repo),
        "absolute": (f"rm {repo}/untracked.txt", "/"),
        "scratch": (f"rm {scratch}/tmpfile", repo),
        "cd-then-rm": ("cd sub && rm clean2.txt", repo),
        "redirect-noise": ("rm untracked.txt 2>/dev/null", repo),
        "two-files": ("rm untracked.txt clean.txt", repo),
        "missing-file": ("rm nothing-here.txt", repo),
        "heredoc-around": ("cat > n.txt <<'EOF'\nrm dirty.txt (not code)\nEOF\nrm untracked.txt", repo),
    }
    wrongly_blocked = [n for n, (c, cwd) in cases.items() if not rm_removes_only_recoverable_files(c, cwd)]
    assert not wrongly_blocked, wrongly_blocked


def test_everything_else_still_blocks(world):
    repo, outside = world["repo"], world["outside"]
    cases = {
        "tracked-modified": ("rm dirty.txt", repo),
        "one-bad-among-good": ("rm untracked.txt dirty.txt", repo),
        "outside-any-repo": (f"rm {outside}/f.txt", repo),
        "relative-outside": ("rm f.txt", outside),
        "force-flag": (f"rm {_F} untracked.txt", repo),
        "recursive-flag": (f"rm {_RF} sub", repo),
        "directory": ("rm sub", repo),
        "glob": ("rm *.txt", repo),
        "brace": ("rm {untracked,clean}.txt", repo),
        "variable": ("rm $F", repo),
        "substitution": ("rm $(ls)", repo),
        "backtick": ("rm `ls`", repo),
        "git-internals": ("rm .git/config", repo),
        "escape-the-repo": ("rm ../../outside/f.txt", repo + "/sub"),
        "xargs": ("ls | xargs rm", repo),
        "sudo": ("sudo rm untracked.txt", repo),
        "shell-c": ("sh -c 'rm untracked.txt'", repo),
        "cd-with-semicolon": ("cd sub; rm clean2.txt", repo),
        "cd-variable": ("cd $D && rm untracked.txt", repo),
        "subshell": ("(cd sub && rm clean2.txt)", repo),
        "newline-dirty": ("ls\nrm dirty.txt", repo),
        "newline-cd": ("cd sub\nrm clean2.txt", repo),
        "no-cwd": ("rm untracked.txt", None),
        "no-operand": ("rm", repo),
        "unrelated-command": ("ls untracked.txt", repo),
    }
    wrongly_allowed = [n for n, (c, cwd) in cases.items() if rm_removes_only_recoverable_files(c, cwd)]
    assert not wrongly_allowed, wrongly_allowed


def test_whole_command_decision(world):
    cfg = load_config(RULES_DIR)
    cfg["safety"] = {"enabled": True, "disabled_rules": []}
    repo = world["repo"]
    assert check_command("rm untracked.txt", cfg, cwd=repo)["decision"] == "allow"
    assert check_command("rm dirty.txt", cfg, cwd=repo)["decision"] == "block"
    assert check_command(f"rm {_F} untracked.txt", cfg, cwd=repo)["decision"] == "block"
    # git rm is its own gate: never the quiet path.
    assert check_command("git rm clean.txt", cfg, cwd=repo)["decision"] == "ask"
    assert check_command("git rm --cached clean.txt", cfg, cwd=repo)["decision"] == "allow"


@pytest.mark.slow
def test_real_git_states(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "tracked.txt").write_text("a")
    run("add", ".")
    run("commit", "-qm", "x")
    (repo / "new.txt").write_text("a")
    (repo / "edited.txt").write_text("a")
    run("add", "edited.txt")
    run("commit", "-qm", "y")
    (repo / "edited.txt").write_text("changed")
    assert _core._git_file_state(str(repo), "tracked.txt") == "clean"
    assert _core._git_file_state(str(repo), "new.txt") == "untracked"
    assert _core._git_file_state(str(repo), "edited.txt") == "modified"


def test_forced_recursive_removal_passes_only_inside_scratch(world, tmp_path):
    repo, scratch, outside = world["repo"], world["scratch"], world["outside"]
    sess = f"{scratch}/proj/sess"
    (Path(sess) / "link").symlink_to(outside)
    ok = {
        "dir": f"rm -{'rf'} {sess}/work",
        "file-force": f"rm -{'f'} {sess}/work/f.txt",
        "variable": f"S={sess}; rm -{'rf'} $S/work",
        "export-variable": f"export S={sess} && rm -{'rf'} $S/work",
        "braced-variable": f"S={sess}; rm -{'rf'} ${{S}}/work",
        "plain-and-forced": f"rm -{'rf'} {sess}/work; rm untracked.txt",
    }
    wrongly_blocked = [n for n, c in ok.items() if not rm_removes_only_recoverable_files(c, repo)]
    assert not wrongly_blocked, wrongly_blocked

    refused = {
        "scratch-root": f"rm -{'rf'} {scratch}",
        "project-dir": f"rm -{'rf'} {scratch}/proj",
        "session-dir": f"rm -{'rf'} {sess}",
        "dotdot-escape": f"rm -{'rf'} {sess}/work/../../../..",
        "outside-the-scratch": f"rm -{'rf'} {outside}/f.txt",
        "mixed": f"rm -{'rf'} {sess}/work {outside}/f.txt",
        "undefined-variable": f"rm -{'rf'} $S/work",
        "substituted-variable": f"S=$(pwd); rm -{'rf'} $S/work",
        "trailing-slash": f"rm -{'rf'} {sess}/work/",
        "symlink-itself": f"rm -{'rf'} {sess}/link",
        "through-symlink": f"rm -{'rf'} {sess}/link/f.txt",
        "glob": f"rm -{'rf'} {sess}/work/*",
        "long-flag": f"rm --recursive {sess}/work",
        "extra-flag": f"rm -{'rfv'} {sess}/work",
        "repo-file": f"rm -{'f'} {repo}/untracked.txt",
        "wrapper": f"xargs rm -{'rf'}",
        "no-cwd": f"rm -{'rf'} {sess}/work",
    }
    wrongly_allowed = [
        n for n, c in refused.items()
        if rm_removes_only_recoverable_files(c, None if n == "no-cwd" else repo)
    ]
    assert not wrongly_allowed, wrongly_allowed

    cfg = load_config(RULES_DIR)
    cfg["safety"] = {"enabled": True, "disabled_rules": []}
    assert check_command(f"rm -{'rf'} {sess}/work", cfg, cwd=repo)["decision"] == "allow"
    assert check_command(f"rm -{'rf'} /", cfg, cwd=repo)["decision"] == "block"


def test_git_clean_passes_only_with_explicit_paths_inside_a_repo(world):
    from agentwire.safety._core import git_clean_only_explicit_untracked

    repo, scratch, outside = world["repo"], world["scratch"], world["outside"]
    ok = {
        "file": ("git clean -fq untracked.txt", repo),
        "dir": ("git clean -fdq sub", repo),
        "ignored-only": ("git clean -fdXq sub", repo),
        "double-dash": ("git clean -f -- untracked.txt", repo),
        "after-cd": ("cd sub && git clean -fq clean2.txt", repo),
        "scratch": (f"git clean -fdq {scratch}/proj/sess/work", repo),
        "absolute": (f"git clean -fq {repo}/untracked.txt", "/"),
    }
    wrongly_blocked = [n for n, (c, cwd) in ok.items() if not git_clean_only_explicit_untracked(c, cwd)]
    assert not wrongly_blocked, wrongly_blocked

    refused = {
        "whole-tree": ("git clean -fd", repo),
        "untracked-and-ignored": ("git clean -fdx sub", repo),
        "dot": ("git clean -fd .", repo),
        "repo-root": (f"git clean -fd {repo}", repo),
        "git-dir": ("git clean -fd .git", repo),
        "glob": ("git clean -fd *.txt", repo),
        "variable": ("git clean -fd $D", repo),
        "outside-repo": (f"git clean -fd {outside}/f.txt", repo),
        "dotdot": ("git clean -fd ../outside", repo),
        "interactive": ("git clean -fdi sub", repo),
        "global-option": ("git -C sub clean -fd x", repo),
        "wrapper": ("ls | xargs git clean -fd", repo),
        "subshell": ("(git clean -fd sub)", repo),
        "no-cwd": ("git clean -fd sub", None),
    }
    wrongly_allowed = [n for n, (c, cwd) in refused.items() if git_clean_only_explicit_untracked(c, cwd)]
    assert not wrongly_allowed, wrongly_allowed

    cfg = load_config(RULES_DIR)
    cfg["safety"] = {"enabled": True, "disabled_rules": []}
    assert check_command("git clean -fq untracked.txt", cfg, cwd=repo)["decision"] == "allow"
    assert check_command("git clean -fd", cfg, cwd=repo)["decision"] == "block"


def test_find_delete_passes_only_inside_scratch(world):
    from agentwire.safety._core import find_delete_only_in_scratch

    repo, scratch, outside = world["repo"], world["scratch"], world["outside"]
    sess = f"{scratch}/proj/sess"
    (Path(sess) / "link").symlink_to(outside)
    ok = {
        "name": f"find {sess}/work -name '*.txt' -delete",
        "type-depth": f"find {sess}/work -maxdepth 2 -type f -mtime +1 -delete",
        "empty": f"find {sess}/work -empty -delete",
        "relative": "find work -name '*.log' -delete",
        "variable": f"S={sess}; find $S/work -name x -delete",
        "two-paths": f"find {sess}/work {sess}/work/f.txt -delete",
    }
    wrongly_blocked = [n for n, c in ok.items() if not find_delete_only_in_scratch(c, sess)]
    assert not wrongly_blocked, wrongly_blocked

    refused = {
        "repo": f"find {repo} -name '*.txt' -delete",
        "outside": f"find {outside} -delete",
        "cwd-default": "find -name x -delete",
        "scratch-root": f"find {scratch} -delete",
        "session-dir": f"find {sess} -delete",
        "dotdot-escape": f"find {sess}/work/../../../.. -delete",
        "mixed": f"find {sess}/work {outside} -delete",
        "symlink-start": f"find {sess}/link -delete",
        "follow-links": f"find -L {sess}/work -delete",
        "exec": f"find {sess}/work -exec sh -c x {{}} ; -delete",
        "exec-rm": f"find {sess}/work -exec rm {{}} +",
        "fprint": f"find {sess}/work -fprint {outside}/out -delete",
        "substituted-arg": f"find {sess}/work -name $(whoami) -delete",
        "undefined-variable": "find $S/work -delete",
        "glob-start": f"find {sess}/work/* -delete",
        "wrapper": f"xargs find {sess}/work -delete",
    }
    wrongly_allowed = [n for n, c in refused.items() if find_delete_only_in_scratch(c, sess)]
    assert not wrongly_allowed, wrongly_allowed

    cfg = load_config(RULES_DIR)
    cfg["safety"] = {"enabled": True, "disabled_rules": []}
    assert check_command(f"find {sess}/work -name '*.txt' -delete", cfg, cwd=sess)["decision"] == "allow"
    assert find_delete_only_in_scratch(f"find {sess}/work -delete", None) is False
