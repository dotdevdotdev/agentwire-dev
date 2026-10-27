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
    _render_scheduler_staleness_section,
)








class TestRenderSchedulerStalenessSection:



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

