"""Scheduler dispatch vs the shared-working-dir guard (#854).

The dispatch path built its `NewArgs` with no `allow_shared_dir` attribute at
all, so `session_cli.cmd_new`'s `getattr(args, "allow_shared_dir", False)` was
always False and any live session sitting in the task's project dir killed every
dispatch into it.
"""

import pytest

from agentwire import ensure_cli
from agentwire.ensure_cli import ENSURE_EXIT_SESSION_ERROR
from agentwire.tasks import parse_task_config


def _task(**overrides):
    return parse_task_config("t", {"prompt": "do the thing", **overrides})


# --- derivation ------------------------------------------------------------


# --- explicit override wins in both directions -----------------------------


# --- wiring: the attribute actually reaches cmd_new ------------------------


class _Ctx:
    attempt = 0
    summary_file = None

    def set_pre_output(self, *_):  # pragma: no cover - unused in these paths
        pass


@pytest.fixture
def captured_new_args(monkeypatch, tmp_path):
    """Drive `_run_ensure_task` far enough to capture the NewArgs it builds."""
    from agentwire import session_cli

    seen = {}
    monkeypatch.setattr(ensure_cli, "tmux_session_exists", lambda _s: False)

    def fake_cmd_new(args):
        seen["args"] = args
        return 1  # bail out right after construction

    monkeypatch.setattr(session_cli, "cmd_new", fake_cmd_new)

    def run(task):
        rc = ensure_cli._run_ensure_task(
            args=None,
            session="memory-manager",
            task=task,
            ctx=_Ctx(),
            shell=None,
            project_path=tmp_path,
            json_mode=True,
        )
        assert rc == ENSURE_EXIT_SESSION_ERROR
        return seen["args"]

    return run


def test_dispatch_keeps_guard_for_git_task(captured_new_args):
    args = captured_new_args(_task(starting_ref="main"))
    assert args.allow_shared_dir is False
