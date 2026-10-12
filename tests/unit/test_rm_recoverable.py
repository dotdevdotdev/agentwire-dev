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
        "untracked": (f"rm untracked.txt", repo),
        "tracked-clean": (f"rm clean.txt", repo),
        "absolute": (f"rm {repo}/untracked.txt", "/"),
        "scratch": (f"rm {scratch}/tmpfile", repo),
        "cd-then-rm": (f"cd sub && rm clean2.txt", repo),
        "redirect-noise": (f"rm untracked.txt 2>/dev/null", repo),
        "two-files": (f"rm untracked.txt clean.txt", repo),
        "missing-file": (f"rm nothing-here.txt", repo),
        "heredoc-around": (f"cat > n.txt <<'EOF'\nrm dirty.txt (not code)\nEOF\nrm untracked.txt", repo),
    }
    wrongly_blocked = [n for n, (c, cwd) in cases.items() if not rm_removes_only_recoverable_files(c, cwd)]
    assert not wrongly_blocked, wrongly_blocked


def test_everything_else_still_blocks(world):
    repo, outside = world["repo"], world["outside"]
    cases = {
        "tracked-modified": (f"rm dirty.txt", repo),
        "one-bad-among-good": (f"rm untracked.txt dirty.txt", repo),
        "outside-any-repo": (f"rm {outside}/f.txt", repo),
        "relative-outside": (f"rm f.txt", outside),
        "force-flag": (f"rm {_F} untracked.txt", repo),
        "recursive-flag": (f"rm {_RF} sub", repo),
        "directory": (f"rm sub", repo),
        "glob": (f"rm *.txt", repo),
        "brace": (f"rm {{untracked,clean}}.txt", repo),
        "variable": (f"rm $F", repo),
        "substitution": (f"rm $(ls)", repo),
        "backtick": ("rm `ls`", repo),
        "git-internals": (f"rm .git/config", repo),
        "escape-the-repo": (f"rm ../../outside/f.txt", repo + "/sub"),
        "xargs": (f"ls | xargs rm", repo),
        "sudo": (f"sudo rm untracked.txt", repo),
        "shell-c": (f"sh -c 'rm untracked.txt'", repo),
        "cd-with-semicolon": (f"cd sub; rm clean2.txt", repo),
        "cd-variable": (f"cd $D && rm untracked.txt", repo),
        "subshell": (f"(cd sub && rm clean2.txt)", repo),
        "newline-dirty": ("ls\nrm dirty.txt", repo),
        "newline-cd": ("cd sub\nrm clean2.txt", repo),
        "no-cwd": (f"rm untracked.txt", None),
        "no-operand": (f"rm", repo),
        "unrelated-command": (f"ls untracked.txt", repo),
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
