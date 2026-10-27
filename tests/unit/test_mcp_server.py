"""Tests for agentwire/mcp_server.py — format functions, run_agentwire_cmd, helpers."""

import subprocess
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Format functions — empty/missing-key/multi-entry behavior is parametrized;
# format-specific assertions kept as individual tests below.
# ---------------------------------------------------------------------------








# Format-specific assertions — keep separate; logic is unique per formatter.









class TestDeliveryResultBehavior:
    """#834: an unverified send must report its inbox fallback, not just
    hand an inert 'check the pane or resend' string back to the caller."""




    def test_unverified_with_inbox_stuck_fallback_warns_to_check_pane(self):
        """#843: "inbox_stuck" (queued, but the original stale draft could
        not be confirmed cleared from the input box) must read as an honest
        warning, distinct from the calm "No action needed" wording used for
        a fully-recovered "inbox" fallback."""
        from agentwire.mcp_core import _delivery_result
        result = _delivery_result({"verified": False, "fallback": "inbox_stuck"}, "to session 'proj'")
        assert "could NOT be confirmed cleared" in result
        assert "Check the pane manually" in result
        assert "No action needed" not in result










# ---------------------------------------------------------------------------
# run_agentwire_cmd
# ---------------------------------------------------------------------------


class TestRunAgentwireCmd:
    def setup_method(self):
        from agentwire.core import run_agentwire_cmd
        self.fn = run_agentwire_cmd

    @patch("agentwire.core._run_bounded")
    def test_successful_json(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"success": true, "data": 1}',
            stderr="",
        )
        result = self.fn(["list"])
        assert result == {"success": True, "data": 1}
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["agentwire", "list", "--json"]




    @patch("agentwire.core._run_bounded")
    def test_nonzero_returncode(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="session not found",
        )
        result = self.fn(["kill", "-s", "x"])
        assert result["success"] is False
        assert "session not found" in result["error"]


    @patch("agentwire.core._run_bounded")
    def test_timeout_expired(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="agentwire", timeout=30)
        result = self.fn(["long-cmd"])
        assert result["success"] is False
        assert "timed out" in result["error"]







# ---------------------------------------------------------------------------
# get_portal_url
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# get_caller_session
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# _portal_request
# ---------------------------------------------------------------------------








class TestRunAgentwireCmdLogsNoArgvBodies:
    """#1113: the slow-command warning and the debug line log verb only, never message text."""

    @patch("agentwire.core._run_bounded")
    def test_no_log_line_carries_the_message_body(self, mock_run, monkeypatch, caplog):
        import logging

        from agentwire import core

        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        monkeypatch.setattr(core, "SLOW_CMD_WARN_SECONDS", -1)
        leaks = []
        for argv in (["say", "SECRET-BODY"], ["notify-parent", "--to", "SECRET-BODY"],
                     ["session", "send", "SECRET-BODY"]):
            caplog.clear()
            with caplog.at_level(logging.DEBUG, logger=core.logger.name):
                core.run_agentwire_cmd(argv)
            if "SECRET-BODY" in caplog.text or not caplog.text:
                leaks.append((argv, caplog.text))
        assert not leaks, leaks


