"""``agentwire helper`` (#838): a worker session sharing the caller's checkout."""

import pytest

from agentwire import worktree_cli


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "myapp"
    root.mkdir()
    return root


@pytest.fixture
def captured_new(monkeypatch):
    """Replace ``session_cli.cmd_new`` and capture the args it's handed."""
    from agentwire import session_cli

    seen = {}

    def fake_cmd_new(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(session_cli, "cmd_new", fake_cmd_new)
    return seen


def _run(repo, captured_new, **kwargs):
    import argparse
    defaults = dict(name="digest", project=str(repo), json=True, prompt=None,
                    roles=None, posture=None, model=None, env=None,
                    created_by=None, caller_session=None, no_cohort=False,
                    no_soul=False)
    defaults.update(kwargs)
    assert worktree_cli.cmd_helper(argparse.Namespace(**defaults)) == 0
    return captured_new["args"]


def test_helper_shares_the_checkout_as_a_worker_without_isolation(repo, captured_new):
    """No worktree, no branch: cmd_new gets the caller's own dir, the shared-dir
    guard declared (not forced), and the worker rail."""
    args = _run(repo, captured_new)
    assert (args.path, args.kind, args.worktree_topology, args.allow_shared_dir, args.force) == (
        str(repo), "worker", False, True, False)
