"""Tests for agentwire/worktree_registry.py — local branch↔session store."""


import pytest

from agentwire import worktree_registry as reg


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway dir for every test."""
    monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path / "wt-registry")


def test_register_and_entries(tmp_path):
    repo = tmp_path / "monorepo"
    reg.register(repo, branch="fix-bug", session="monorepo-fix-bug",
                 base="develop", worktree_path=tmp_path / "wt" / "monorepo-fix-bug")
    rows = reg.entries(repo)
    assert len(rows) == 1
    e = rows[0]
    assert e["branch"] == "fix-bug"
    assert e["session"] == "monorepo-fix-bug"
    assert e["base"] == "develop"
    assert e["created_at"]  # stamped


def test_register_is_idempotent_per_session(tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt" / "repo-a"
    reg.register(repo, branch="a", session="repo-a", base="main", worktree_path=wt)
    reg.register(repo, branch="a", session="repo-a", base="develop", worktree_path=wt)
    rows = reg.entries(repo)
    assert len(rows) == 1
    assert rows[0]["base"] == "develop"  # replaced, not duplicated


def test_many_sessions_one_repo(tmp_path):
    """Monorepo: several branches of the SAME repo coexist."""
    repo = tmp_path / "monorepo"
    for n in ("one", "two", "three"):
        reg.register(repo, branch=n, session=f"monorepo-{n}", base="develop",
                     worktree_path=tmp_path / "wt" / f"monorepo-{n}")
    sessions = {e["session"] for e in reg.entries(repo)}
    assert sessions == {"monorepo-one", "monorepo-two", "monorepo-three"}


def test_unregister_by_session(tmp_path):
    repo = tmp_path / "repo"
    reg.register(repo, branch="a", session="repo-a", base="main",
                 worktree_path=tmp_path / "wt" / "repo-a")
    reg.register(repo, branch="b", session="repo-b", base="main",
                 worktree_path=tmp_path / "wt" / "repo-b")
    removed = reg.unregister(repo, session="repo-a")
    assert removed == 1
    assert {e["session"] for e in reg.entries(repo)} == {"repo-b"}


def test_all_entries_across_repos(tmp_path):
    reg.register(tmp_path / "alpha", branch="x", session="alpha-x", base="main",
                 worktree_path=tmp_path / "wt" / "alpha-x")
    reg.register(tmp_path / "beta", branch="y", session="beta-y", base="dev",
                 worktree_path=tmp_path / "wt" / "beta-y")
    rows = reg.all_entries()
    assert len(rows) == 2
    # Each row is tagged with its repo path.
    assert all(r.get("project") for r in rows)


def test_entries_empty_for_unknown_repo(tmp_path):
    assert reg.entries(tmp_path / "never-seen") == []


def test_corrupt_file_is_tolerated(tmp_path):
    repo = tmp_path / "repo"
    path = reg.registry_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json")
    assert reg.entries(repo) == []  # degrades, doesn't raise


def test_concurrent_registration_threads_all_survive(tmp_path):
    """N threads registering distinct sessions must not clobber each other."""
    import threading

    repo = tmp_path / "monorepo"
    n = 20
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()  # maximize contention on the read-modify-write
        reg.register(repo, branch=f"b{i}", session=f"monorepo-{i}", base="develop",
                     worktree_path=tmp_path / "wt" / f"monorepo-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    sessions = {e["session"] for e in reg.entries(repo)}
    assert sessions == {f"monorepo-{i}" for i in range(n)}


def test_concurrent_registration_processes_all_survive(tmp_path, monkeypatch):
    """Cross-PROCESS contention (the real dispatch case) — flock must serialize."""
    import subprocess as sp
    import sys

    registry_dir = tmp_path / "wt-registry"
    repo = tmp_path / "monorepo"
    wt = tmp_path / "wt"
    n = 8

    # Each child registers one entry, pointing REGISTRY_DIR at the shared dir.
    prog = (
        "import sys\n"
        "from pathlib import Path\n"
        "from agentwire import worktree_registry as reg\n"
        "reg.REGISTRY_DIR = Path(sys.argv[1])\n"
        "i = sys.argv[2]\n"
        "reg.register(Path(sys.argv[3]), branch='b'+i, session='monorepo-'+i, "
        "base='develop', worktree_path=Path(sys.argv[4])/('monorepo-'+i))\n"
    )
    procs = [
        sp.Popen([sys.executable, "-c", prog, str(registry_dir), str(i), str(repo), str(wt)])
        for i in range(n)
    ]
    for p in procs:
        assert p.wait() == 0

    monkeypatch.setattr(reg, "REGISTRY_DIR", registry_dir)
    sessions = {e["session"] for e in reg.entries(repo)}
    assert sessions == {f"monorepo-{i}" for i in range(n)}


def test_file_is_hand_editable_json(tmp_path):
    repo = tmp_path / "repo"
    reg.register(repo, branch="a", session="repo-a", base="main",
                 worktree_path=tmp_path / "wt" / "repo-a")
    import json
    data = json.loads(reg.registry_file(repo).read_text())
    assert data["entries"][0]["session"] == "repo-a"
    assert data["project"].endswith("repo")
