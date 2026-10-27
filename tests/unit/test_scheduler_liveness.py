"""Scheduler daemon liveness + single-dispatcher guard (#873).

Liveness used to be "does the tmux session `agentwire-scheduler` exist?", which
is false for a daemon supervised outside tmux (launchd). Two consequences, both
covered here:

1. A running daemon reported as `stopped`, and `doctor` skipped its staleness
   check — the diagnostic that would catch a wedged daemon, disabled exactly
   when it was needed.
2. Nothing refused a second dispatcher: the portal autostarted its own daemon
   next to the launchd one, and the board double-dispatched.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from agentwire.scheduler.report import (
    _pid_is_scheduler,
    live_daemon_state,
)


@pytest.fixture
def live_state_file(tmp_path, monkeypatch):
    """Point the scheduler's live-state path at a temp file."""
    path = tmp_path / "scheduler-live.json"
    cfg = MagicMock()
    cfg.live_state_file = path
    monkeypatch.setattr("agentwire.scheduler._sched_config", lambda: cfg)
    return path




class TestPidIsScheduler:







    def test_recycled_pid_running_something_else_is_false(self):
        """PID reuse must not read as "the scheduler is running"."""
        with patch("agentwire.scheduler.report.os.kill", return_value=None), \
             patch("agentwire.scheduler.report.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="/usr/sbin/cupsd -l")
            assert _pid_is_scheduler(12345) is False



class TestLiveDaemonState:

    def test_leftover_file_from_stopped_daemon_is_not_live(self, live_state_file):
        """The requirement the tmux gate existed to satisfy, kept."""
        live_state_file.write_text(json.dumps({"status": "running", "pid": 999999}))
        with patch("agentwire.scheduler.report._pid_is_scheduler", return_value=False):
            assert live_daemon_state() is None





class TestStatusReportsLivenessNotTmux:
    """The headline symptom of #873, pinned.

    `scheduler status` printed `Scheduler: stopped` while the daemon was
    actively dispatching, because it asked tmux. These tests fail if
    `cmd_scheduler_status` goes back to `tmux_session_exists` in either
    direction — a launchd daemon must read running, and a leftover state file
    must not.
    """

    def _run_status(self, tmp_path, live, in_tmux, json_mode=False):
        from agentwire.scheduler_cli import cmd_scheduler_status

        board_path = tmp_path / "scheduler.yaml"
        board_path.write_text("tasks: {}\n")
        cfg = MagicMock()
        cfg.scheduler.board_file = board_path
        board = MagicMock()
        board.tasks = {}

        args = MagicMock()
        args.json = json_mode

        with patch("agentwire.config.get_config", return_value=cfg), \
             patch("agentwire.scheduler.live_daemon_state", return_value=live), \
             patch("agentwire.scheduler.load_board", return_value=board), \
             patch("agentwire.scheduler.pick_next_task", return_value=(None, 60.0)), \
             patch("agentwire.scheduler.read_events", return_value=[]), \
             patch("agentwire.scheduler_cli.tmux_session_exists", return_value=in_tmux):
            rc = cmd_scheduler_status(args)
        return rc

    def test_launchd_daemon_reports_running_with_no_tmux_session(self, tmp_path, capsys):
        self._run_status(
            tmp_path,
            live={"pid": 4242, "started_at": "2026-08-04T13:03:47Z"},
            in_tmux=False,
        )
        out = capsys.readouterr().out
        assert "Scheduler: running" in out
        assert "stopped" not in out
        assert "4242" in out
        assert "external supervisor" in out






class TestServeRefusesASecondDispatcher:
    def _args(self, force=False):
        ns = MagicMock()
        ns.force = force
        return ns

    def test_refuses_when_a_daemon_is_already_live(self, capsys):
        from agentwire.scheduler_cli import cmd_scheduler_serve

        with patch("agentwire.scheduler.live_daemon_state",
                   return_value={"pid": 4242, "started_at": "2026-08-04T13:03:47Z"}), \
             patch("agentwire.scheduler.run_scheduler_loop") as loop:
            rc = cmd_scheduler_serve(self._args())
        assert rc == 1
        loop.assert_not_called()
        err = capsys.readouterr().err
        assert "4242" in err
        assert "second dispatcher" in err






