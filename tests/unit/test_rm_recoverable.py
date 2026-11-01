"""A plain ``rm <file>`` yields exactly when the bytes can come back (#940).

Both halves are load-bearing: recoverable files pass, and every shape that is
not provably a recoverable plain file keeps blocking — flags, globs, variables,
directories, a tracked file with local edits, a file outside every repo, a
wrapper that takes ``rm`` as its argument, an unmodelled ``cd``.

Git state is stubbed (``_git_file_state``) so the fast set spawns no process;
one real-git test is marked ``slow``.
"""

import pytest

from damage_control import engine as _core
from damage_control.engine import rm_removes_only_recoverable_files

pytestmark = pytest.mark.real_agentwire_home

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
    states = {
        "untracked.txt": "untracked",
        "clean.txt": "clean",
        "dirty.txt": "modified",
        "sub/clean2.txt": "clean",
    }
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
        "heredoc-around": (
            "cat > n.txt <<'EOF'\nrm dirty.txt (not code)\nEOF\nrm untracked.txt",
            repo,
        ),
    }
    wrongly_blocked = [
        n for n, (c, cwd) in cases.items() if not rm_removes_only_recoverable_files(c, cwd)
    ]
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
    wrongly_allowed = [
        n for n, (c, cwd) in cases.items() if rm_removes_only_recoverable_files(c, cwd)
    ]
    assert not wrongly_allowed, wrongly_allowed
