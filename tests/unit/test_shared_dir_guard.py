"""Shared-dir conflict guard (#618): infra services co-reside, agents conflict."""

import os

from agentwire.session_cli import _shared_dir_conflicts

REPO = os.path.realpath(os.getcwd())


def _panes(*pairs: tuple[str, str]) -> str:
    return "\n".join(f"{s}\t{p}" for s, p in pairs)


def test_dev_orchestrator_still_conflicts():
    # The hardcoded `agentwire` dev session is NOT a service — must still trip.
    out = _panes(("agentwire", REPO), ("agentwire-portal", REPO))
    assert _shared_dir_conflicts(out, "agentwire-dev", REPO) == {"agentwire"}
