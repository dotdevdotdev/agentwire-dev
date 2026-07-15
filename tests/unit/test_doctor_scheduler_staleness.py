"""Tests for the doctor scheduler-daemon-staleness check (#803).

`agentwire scheduler serve` is a long-running Python process that imports
its modules once at start — `agentwire rebuild` updates the on-disk package
but can't touch an already-running interpreter's loaded bytecode. A daemon
that predates the last rebuild silently runs stale dispatch logic. Doctor
must surface exactly that state.
"""

import time
from unittest.mock import patch

from agentwire.doctor_cli import (
    _newest_installed_source_mtime,
    _render_scheduler_staleness_section,
    _scheduler_daemon_started_at,
)


class TestSchedulerDaemonStartedAt:
    def test_not_running_returns_none(self):
        with patch("agentwire.doctor_cli.tmux_session_exists", return_value=False):
            assert _scheduler_daemon_started_at() is None

    def test_running_but_no_live_state_returns_none(self):
        with patch("agentwire.doctor_cli.tmux_session_exists", return_value=True), \
             patch("agentwire.scheduler.read_live_state", return_value=None):
            assert _scheduler_daemon_started_at() is None

    def test_running_but_no_started_at_field_returns_none(self):
        with patch("agentwire.doctor_cli.tmux_session_exists", return_value=True), \
             patch("agentwire.scheduler.read_live_state", return_value={"status": "running"}):
            assert _scheduler_daemon_started_at() is None

    def test_running_reads_started_at_from_live_state(self):
        from datetime import datetime, timedelta, timezone
        started = datetime.now(timezone.utc) - timedelta(hours=1)
        with patch("agentwire.doctor_cli.tmux_session_exists", return_value=True), \
             patch("agentwire.scheduler.read_live_state",
                   return_value={"started_at": started.isoformat()}):
            result = _scheduler_daemon_started_at()
        assert result is not None
        assert abs(result - started.timestamp()) < 1

    def test_unparseable_started_at_returns_none(self):
        with patch("agentwire.doctor_cli.tmux_session_exists", return_value=True), \
             patch("agentwire.scheduler.read_live_state",
                   return_value={"started_at": "not-a-date"}):
            assert _scheduler_daemon_started_at() is None


class TestNewestInstalledSourceMtime:
    def test_finds_newest_py_file(self, tmp_path):
        old = tmp_path / "a.py"
        old.write_text("x = 1\n")
        new = tmp_path / "sub" / "b.py"
        new.parent.mkdir()
        new.write_text("y = 2\n")

        import os
        old_time = time.time() - 100
        new_time = time.time()
        os.utime(old, (old_time, old_time))
        os.utime(new, (new_time, new_time))

        result = _newest_installed_source_mtime(tmp_path)
        assert abs(result - new_time) < 1

    def test_empty_dir_returns_zero(self, tmp_path):
        assert _newest_installed_source_mtime(tmp_path) == 0.0

    def test_missing_dir_returns_zero(self, tmp_path):
        assert _newest_installed_source_mtime(tmp_path / "nope") == 0.0


class TestRenderSchedulerStalenessSection:
    def test_daemon_not_running(self, capsys):
        with patch("agentwire.doctor_cli._scheduler_daemon_started_at", return_value=None):
            count = _render_scheduler_staleness_section()
        assert count == 0
        assert "not running" in capsys.readouterr().out

    def test_daemon_current(self, capsys):
        now = time.time()
        with patch("agentwire.doctor_cli._scheduler_daemon_started_at", return_value=now), \
             patch("agentwire.doctor_cli._newest_installed_source_mtime", return_value=now - 100):
            count = _render_scheduler_staleness_section()
        assert count == 0
        assert "[ok]" in capsys.readouterr().out

    def test_daemon_stale_flags_and_suggests_restart(self, capsys):
        started_at = time.time() - (11 * 86400)  # 11 days ago
        newest_src = time.time() - 3600           # rebuilt an hour ago
        with patch("agentwire.doctor_cli._scheduler_daemon_started_at", return_value=started_at), \
             patch("agentwire.doctor_cli._newest_installed_source_mtime", return_value=newest_src):
            count = _render_scheduler_staleness_section()
        out = capsys.readouterr().out
        assert count == 1
        assert "[!!]" in out
        assert "11.0d" in out
        assert "agentwire scheduler stop && agentwire scheduler start" in out

    def test_no_installed_source_found_treated_as_ok(self, capsys):
        # newest_src == 0.0 (couldn't determine) — don't false-positive.
        now = time.time()
        with patch("agentwire.doctor_cli._scheduler_daemon_started_at", return_value=now), \
             patch("agentwire.doctor_cli._newest_installed_source_mtime", return_value=0.0):
            count = _render_scheduler_staleness_section()
        assert count == 0
