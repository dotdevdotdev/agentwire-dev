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
import os
from unittest.mock import MagicMock, patch

import pytest

from agentwire.scheduler.report import (
    _pid_is_scheduler,
    _write_live_state,
    live_daemon_state,
    read_live_state,
)


@pytest.fixture
def live_state_file(tmp_path, monkeypatch):
    """Point the scheduler's live-state path at a temp file."""
    path = tmp_path / "scheduler-live.json"
    cfg = MagicMock()
    cfg.live_state_file = path
    monkeypatch.setattr("agentwire.scheduler._sched_config", lambda: cfg)
    return path


class TestWriteLiveStateStampsPid:
    def test_pid_recorded_on_every_write(self, live_state_file):
        _write_live_state(status="running", started_at="2026-08-04T13:03:47Z")
        data = json.loads(live_state_file.read_text())
        assert data["pid"] == os.getpid()
        assert data["status"] == "running"

    def test_pid_is_refreshed_not_carried(self, live_state_file):
        _write_live_state(status="running")
        _write_live_state(status="running", pid=999999)
        # The writer's real PID always wins — a caller can't spoof it.
        assert json.loads(live_state_file.read_text())["pid"] == os.getpid()


class TestPidIsScheduler:
    def test_own_pid_running_a_scheduler_cmdline(self):
        with patch("agentwire.scheduler.report.subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0, stdout="/usr/bin/python agentwire scheduler serve")
            assert _pid_is_scheduler(os.getpid()) is True

    def test_serve_with_flags_still_qualifies(self):
        with patch("agentwire.scheduler.report.os.kill", return_value=None), \
             patch("agentwire.scheduler.report.subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0,
                stdout="/opt/venv/bin/python3 /usr/local/bin/agentwire scheduler serve --force\n")
            assert _pid_is_scheduler(12345) is True

    def test_module_invocation_qualifies(self):
        with patch("agentwire.scheduler.report.os.kill", return_value=None), \
             patch("agentwire.scheduler.report.subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0, stdout="python -m agentwire scheduler serve")
            assert _pid_is_scheduler(12345) is True

    @pytest.mark.parametrize("cmdline", [
        # The reproduction from review: a recycled PID running a READ-ONLY
        # scheduler command. Substring-matching "scheduler" accepted this,
        # which re-created the false-stale reading AND made serve/start/
        # autostart all refuse — a board with no dispatcher.
        "/usr/local/bin/agentwire scheduler live --watch",
        "/usr/local/bin/agentwire scheduler status",
        "/usr/local/bin/agentwire scheduler board",
        "/usr/local/bin/agentwire scheduler run memory-manager",
        # A path that merely contains the words.
        "/Users/x/scheduler/serve-helper.sh",
        "vim /etc/scheduler-serve.conf",
    ])
    def test_non_dispatcher_processes_are_rejected(self, cmdline):
        with patch("agentwire.scheduler.report.os.kill", return_value=None), \
             patch("agentwire.scheduler.report.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout=cmdline)
            assert _pid_is_scheduler(12345) is False

    def test_dead_pid_is_false(self):
        # PID 0 / negative are never valid targets for a liveness probe.
        assert _pid_is_scheduler(0) is False
        assert _pid_is_scheduler(-1) is False

    def test_lookup_error_means_dead(self):
        with patch("agentwire.scheduler.report.os.kill", side_effect=ProcessLookupError):
            assert _pid_is_scheduler(12345) is False

    def test_permission_error_means_alive(self):
        with patch("agentwire.scheduler.report.os.kill", side_effect=PermissionError):
            assert _pid_is_scheduler(12345) is True

    def test_recycled_pid_running_something_else_is_false(self):
        """PID reuse must not read as "the scheduler is running"."""
        with patch("agentwire.scheduler.report.os.kill", return_value=None), \
             patch("agentwire.scheduler.report.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="/usr/sbin/cupsd -l")
            assert _pid_is_scheduler(12345) is False

    def test_ps_unavailable_keeps_the_kill_answer(self):
        with patch("agentwire.scheduler.report.os.kill", return_value=None), \
             patch("agentwire.scheduler.report.subprocess.run", side_effect=OSError):
            assert _pid_is_scheduler(12345) is True


class TestLiveDaemonState:
    def test_no_state_file(self, live_state_file):
        assert read_live_state() is None
        assert live_daemon_state() is None

    def test_leftover_file_from_stopped_daemon_is_not_live(self, live_state_file):
        """The requirement the tmux gate existed to satisfy, kept."""
        live_state_file.write_text(json.dumps({"status": "running", "pid": 999999}))
        with patch("agentwire.scheduler.report._pid_is_scheduler", return_value=False):
            assert live_daemon_state() is None

    def test_state_without_pid_cannot_be_verified(self, live_state_file):
        live_state_file.write_text(json.dumps({"status": "running"}))
        assert live_daemon_state() is None

    def test_live_pid_returns_the_state(self, live_state_file):
        live_state_file.write_text(
            json.dumps({"status": "running", "pid": 4242, "started_at": "x"}))
        with patch("agentwire.scheduler.report._pid_is_scheduler", return_value=True):
            state = live_daemon_state()
        assert state is not None
        assert state["pid"] == 4242

    def test_daemon_outside_tmux_reads_as_running(self, live_state_file):
        """The launchd case: no tmux session anywhere, daemon very much alive."""
        live_state_file.write_text(json.dumps({"status": "running", "pid": 4242}))
        with patch("agentwire.scheduler.report._pid_is_scheduler", return_value=True), \
             patch("agentwire.core.tmux_session_exists", return_value=False):
            assert live_daemon_state() is not None


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

    def test_launchd_daemon_reports_running_in_json(self, tmp_path, capsys):
        self._run_status(
            tmp_path, live={"pid": 4242}, in_tmux=False, json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["running"] is True
        assert data["pid"] == 4242
        assert data["in_tmux"] is False

    def test_tmux_hosted_daemon_is_labelled_as_such(self, tmp_path, capsys):
        self._run_status(tmp_path, live={"pid": 77}, in_tmux=True)
        out = capsys.readouterr().out
        assert "Scheduler: running" in out
        assert "tmux" in out
        assert "external supervisor" not in out

    def test_leftover_state_with_a_live_tmux_session_still_reads_stopped(
            self, tmp_path, capsys):
        """The other direction: deleting the gate outright must not pass either.

        tmux says the session exists; the PID behind the state file does not
        resolve to a dispatcher, so the daemon is not running.
        """
        self._run_status(tmp_path, live=None, in_tmux=True)
        out = capsys.readouterr().out
        assert "Scheduler: stopped" in out

    def test_nothing_running_reports_stopped(self, tmp_path, capsys):
        self._run_status(tmp_path, live=None, in_tmux=False)
        assert "Scheduler: stopped" in capsys.readouterr().out


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

    def test_force_overrides(self):
        from agentwire.scheduler_cli import cmd_scheduler_serve

        with patch("agentwire.scheduler.live_daemon_state", return_value={"pid": 4242}), \
             patch("agentwire.scheduler.run_scheduler_loop") as loop:
            rc = cmd_scheduler_serve(self._args(force=True))
        assert rc == 0
        loop.assert_called_once()

    def test_starts_when_nothing_is_running(self):
        from agentwire.scheduler_cli import cmd_scheduler_serve

        with patch("agentwire.scheduler.live_daemon_state", return_value=None), \
             patch("agentwire.scheduler.run_scheduler_loop") as loop:
            rc = cmd_scheduler_serve(self._args())
        assert rc == 0
        loop.assert_called_once()


class TestStartAndStopSeeNonTmuxDaemons:
    def test_start_refuses_when_daemon_runs_outside_tmux(self, capsys):
        from agentwire.scheduler_cli import cmd_scheduler_start

        with patch("agentwire.scheduler_cli._check_tmux_installed", return_value=True), \
             patch("agentwire.scheduler.live_daemon_state", return_value={"pid": 4242}), \
             patch("agentwire.scheduler_cli.tmux_session_exists", return_value=False), \
             patch("agentwire.scheduler_cli.subprocess.run") as run:
            rc = cmd_scheduler_start(MagicMock())
        assert rc == 1
        run.assert_not_called()
        assert "Refusing to start a second dispatcher" in capsys.readouterr().out

    def test_stop_reports_an_external_daemon_honestly(self, capsys):
        from agentwire.scheduler_cli import cmd_scheduler_stop

        with patch("agentwire.scheduler.live_daemon_state", return_value={"pid": 4242}), \
             patch("agentwire.scheduler_cli.tmux_session_exists", return_value=False), \
             patch("agentwire.scheduler_cli.subprocess.run") as run:
            rc = cmd_scheduler_stop(MagicMock())
        assert rc == 1
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "outside tmux" in out
        assert "not running" not in out

    def test_stop_still_reports_a_genuinely_stopped_daemon(self, capsys):
        from agentwire.scheduler_cli import cmd_scheduler_stop

        with patch("agentwire.scheduler.live_daemon_state", return_value=None), \
             patch("agentwire.scheduler_cli.tmux_session_exists", return_value=False):
            rc = cmd_scheduler_stop(MagicMock())
        assert rc == 1
        assert "not running" in capsys.readouterr().out


class TestPortalAutostartGuard:
    """The portal must not add a dispatcher next to an externally-supervised one."""

    def _server(self):
        from agentwire.routes.scheduler import SchedulerRoutesMixin

        class _S(SchedulerRoutesMixin):
            pass

        return _S()

    @pytest.mark.asyncio
    async def test_skips_autostart_when_a_daemon_runs_outside_tmux(self, caplog):
        import logging

        server = self._server()
        with patch("agentwire.scheduler.live_daemon_state",
                   return_value={"pid": 4242, "started_at": "2026-07-27T00:00:00Z"}), \
             patch("asyncio.create_subprocess_exec") as spawn, \
             caplog.at_level(logging.INFO, logger="agentwire.routes.scheduler"):
            started = await server._start_scheduler_daemon()
        assert started is False
        spawn.assert_not_called()
        # Skipping is logged, not silent — otherwise the only evidence the
        # portal declined is a board that doesn't double-dispatch.
        assert "4242" in caplog.text

    @pytest.mark.asyncio
    async def test_starts_when_no_daemon_is_live(self):
        server = self._server()
        proc = MagicMock()

        async def _wait():
            return 0

        proc.wait = _wait

        async def _spawn(*a, **kw):
            return proc

        with patch("agentwire.scheduler.live_daemon_state", return_value=None), \
             patch("asyncio.create_subprocess_exec", side_effect=_spawn) as spawn:
            started = await server._start_scheduler_daemon()
        assert started is True
        assert spawn.call_count == 2

    @pytest.mark.asyncio
    async def test_is_running_no_longer_asks_tmux(self):
        server = self._server()
        with patch("agentwire.scheduler.live_daemon_state", return_value={"pid": 1}), \
             patch("asyncio.create_subprocess_exec") as spawn:
            assert await server._is_scheduler_running() is True
        spawn.assert_not_called()
