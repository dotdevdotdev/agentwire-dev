"""A remote launch records the same identity a local one does (#886).

#871 made every session-launch path record its identity, but the remote
``cmd_new`` branch passed only ``created_via`` — so an explicit
``--created-by`` was silently dropped and the session's ROLE
(orchestrator/worker/reviewer) was never written. The other remote paths
(``recreate`` / ``fork`` / ``history resume``) already pass ``role`` and have
no ``--created-by`` flag of their own, local or remote; the asymmetry was
``new``'s alone. These tests pin both halves.
"""

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentwire import session_cli as m


@pytest.fixture
def remote_new(monkeypatch):
    """Run ``cmd_new`` down its remote branch, capturing the recorded identity."""
    recorded = {}

    monkeypatch.setattr(m, "_check_tmux_installed", lambda: True)
    monkeypatch.setattr(m, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(m, "resolve_roles", lambda *a, **k: [])
    monkeypatch.setattr(m, "inject_soul", lambda names, cfg, no_soul=False: [])
    monkeypatch.setattr(m, "_resolve_posture_from_args", lambda a, **kw: ("bypass", None))
    monkeypatch.setattr(m, "_get_machine_config", lambda mid: {"host": "gpu.local"})
    monkeypatch.setattr(
        m, "build_agent_command",
        lambda *a, **k: SimpleNamespace(command="claude", env={}))
    monkeypatch.setattr(m, "mirror_role_prompt_remote", lambda agent, mid, cmd: cmd)
    def fake_run_remote(machine_id, cmd, *a, **k):
        # `test -d <worktree>` → 0 (already there, nothing to create);
        # `tmux has-session` → 1 (no session by that name yet).
        return MagicMock(returncode=0 if cmd.startswith("test -d") else 1)

    monkeypatch.setattr(m, "_run_remote", fake_run_remote)
    monkeypatch.setattr(
        m, "_launch_tmux_session", lambda *a, **k: MagicMock(returncode=0))
    monkeypatch.setattr(m, "_notify_portal_sessions_changed", lambda: None)

    def fake_record(session_name, agent, cwd, **kwargs):
        recorded.update({"session": session_name, "cwd": cwd, **kwargs})
        return {}

    monkeypatch.setattr(m, "record_session_launch", fake_record)

    def run(**overrides):
        args = argparse.Namespace(
            session="proj@gpu", path=None, force=False, json=True,
            roles=None, no_soul=True, first_message=None, env=None,
            created_by=None, kind=None, model=None, base=None, pull_first=None,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        assert m.cmd_new(args) == 0
        return recorded

    return run


def test_remote_launch_records_the_session_role(remote_new):
    """A branchless remote name is an orchestrator — and must say so on disk."""
    assert remote_new()["role"] == "orchestrator"


def test_remote_worktree_launch_records_the_worker_role(remote_new):
    assert remote_new(session="proj/fix-bug@gpu")["role"] == "worker"


def test_remote_launch_honors_an_explicit_created_by(remote_new):
    """``--created-by`` is a user instruction, not a local-only nicety."""
    assert remote_new(created_by="orch")["created_by"] == "orch"


def test_remote_launch_marks_an_explicit_orchestrator_rootless(remote_new):
    """Same joint default as local (#716): --kind orchestrator roots itself."""
    assert remote_new(kind="orchestrator")["created_by"] == ""


def test_remote_launch_defaults_to_no_opinion_about_parentage(remote_new):
    """Cross-machine same-project inheritance is not resolvable, so don't guess.

    ``None`` (no opinion) rather than ``''`` (explicitly rootless): the record
    is keyed by session NAME with no machine in it, and ``''`` would clobber a
    parent recorded by some earlier launch of the same name.
    """
    assert remote_new()["created_by"] is None


def test_remote_launch_still_marks_itself_remote(remote_new):
    """`remote=True` keeps the local git identity out of a remote record."""
    assert remote_new()["remote"] is True
