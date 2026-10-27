"""Tests for agentwire/worktree_registry.py — local branch↔session store."""


import pytest

from agentwire import worktree_registry as reg


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway dir for every test."""
    monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path / "wt-registry")














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




