"""Scheduler dispatch vs the shared-working-dir guard (#854).

The dispatch path built its `NewArgs` with no `allow_shared_dir` attribute at
all, so `session_cli.cmd_new`'s `getattr(args, "allow_shared_dir", False)` was
always False and any live session sitting in the task's project dir killed every
dispatch into it.
"""

from types import SimpleNamespace

import pytest

from agentwire import ensure_cli
from agentwire.ensure_cli import ENSURE_EXIT_SESSION_ERROR, _dispatch_shares_dir
from agentwire.tasks import parse_task_config


def _task(**overrides):
    return parse_task_config("t", {"prompt": "do the thing", **overrides})


# --- derivation ------------------------------------------------------------


def test_branchless_task_shares_dir():
    # No starting_ref => the dispatch does no branch work in the tree.
    assert _dispatch_shares_dir(_task()) is True


def test_starting_ref_task_keeps_guard():
    # starting_ref => checkout/work-branch/reset — exactly what the guard is for.
    assert _dispatch_shares_dir(_task(starting_ref="main")) is False


# --- explicit override wins in both directions -----------------------------


def test_explicit_false_rearms_guard_on_branchless_task():
    assert _dispatch_shares_dir(_task(allow_shared_dir=False)) is False


def test_explicit_true_opens_guard_on_git_task():
    assert _dispatch_shares_dir(
        _task(starting_ref="main", allow_shared_dir=True)
    ) is True


def test_unset_is_none_not_false():
    # None must mean "derive", so a missing key can't be confused with `false`.
    assert _task().allow_shared_dir is None
    assert _task(allow_shared_dir=False).allow_shared_dir is False


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
            args=None, session="memory-manager", task=task, ctx=_Ctx(),
            shell=None, project_path=tmp_path, json_mode=True,
        )
        assert rc == ENSURE_EXIT_SESSION_ERROR
        return seen["args"]

    return run


def test_dispatch_passes_allow_shared_dir_for_branchless_task(captured_new_args):
    args = captured_new_args(_task())
    assert args.allow_shared_dir is True
    # Never via --force: that would kill-replace an existing same-name session.
    assert args.force is False


def test_dispatch_keeps_guard_for_git_task(captured_new_args):
    args = captured_new_args(_task(starting_ref="main"))
    assert args.allow_shared_dir is False


class TestPaneDiagnosis:
    """#856: `Agent not running in session '<name>'` names a session that the
    zombie reaper deletes 60s later — the message must carry the evidence."""

    def test_reports_pane_command_and_last_line(self, monkeypatch):
        monkeypatch.setattr(
            ensure_cli.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="zsh\n"),
        )
        monkeypatch.setattr(
            ensure_cli.pane_manager, "capture_pane",
            lambda *a, **k: 'cd /tmp/wt && claude \\\n\n  --append-system-prompt "$(</var/f\n',
        )
        out = ensure_cli._pane_diagnosis("proj/scheduler-a-1")
        assert "pane=zsh" in out
        assert "--append-system-prompt" in out

    def test_degrades_to_empty_when_tmux_cannot_answer(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("no tmux")

        monkeypatch.setattr(ensure_cli.subprocess, "run", boom)
        monkeypatch.setattr(ensure_cli.pane_manager, "capture_pane", boom)
        assert ensure_cli._pane_diagnosis("proj/scheduler-a-1") == ""

    def test_long_lines_are_truncated(self, monkeypatch):
        monkeypatch.setattr(
            ensure_cli.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout=""),
        )
        monkeypatch.setattr(
            ensure_cli.pane_manager, "capture_pane", lambda *a, **k: "x" * 5000,
        )
        assert len(ensure_cli._pane_diagnosis("s")) < 300
