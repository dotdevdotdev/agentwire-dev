"""Integration tests for MCP tools — verify CLI args construction and result formatting."""

from unittest.mock import patch

import pytest


def _success(**extra):
    return {"success": True, **extra}


def _failure(error="something broke"):
    return {"success": False, "error": error}


# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------


class TestSessionTools:
    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_sessions_list_success(self, mock_cmd):
        from agentwire.mcp_server import sessions_list
        mock_cmd.return_value = _success(sessions=[
            {"name": "app", "machine": "local", "windows": 1, "path": "/p", "type": "claude-bypass"},
        ])
        result = sessions_list()
        mock_cmd.assert_called_once_with(["list", "--sessions"])
        assert "app" in result

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_sessions_list_failure(self, mock_cmd):
        from agentwire.mcp_server import sessions_list
        mock_cmd.return_value = _failure("tmux not running")
        result = sessions_list()
        assert "Failed" in result

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_session_create_minimal(self, mock_cmd):
        from agentwire.mcp_server import session_create
        mock_cmd.return_value = _success()
        result = session_create(name="test")
        mock_cmd.assert_called_once_with(["new", "-s", "test"])
        assert "created" in result

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_session_create_all_args(self, mock_cmd):
        from agentwire.mcp_server import session_create
        mock_cmd.return_value = _success()
        session_create(name="x", project_dir="/p", roles="voice,worker", session_type="bare")
        args = mock_cmd.call_args[0][0]
        assert args == ["new", "-s", "x", "-p", "/p", "--roles", "voice,worker", "--type", "bare"]

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    @patch("agentwire.mcp_server.get_caller_session", return_value="orchestrator")
    def test_session_send_cross_session(self, mock_caller, mock_cmd):
        from agentwire.mcp_server import session_send
        mock_cmd.return_value = _success()
        session_send(session="worker", message="do task")
        sent_msg = mock_cmd.call_args[0][0][3]  # args[3] = message
        assert "[From: orchestrator]" in sent_msg

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    @patch("agentwire.mcp_server.get_caller_session", return_value=None)
    def test_session_send_no_caller(self, mock_caller, mock_cmd):
        from agentwire.mcp_server import session_send
        mock_cmd.return_value = _success()
        session_send(session="target", message="hello")
        sent_msg = mock_cmd.call_args[0][0][3]
        assert sent_msg == "hello"


# ---------------------------------------------------------------------------
# Pane tools
# ---------------------------------------------------------------------------


class TestPaneTools:
    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_pane_send_with_session(self, mock_cmd):
        from agentwire.mcp_server import pane_send
        mock_cmd.return_value = _success()
        pane_send(pane=1, message="task", session="my-session")
        args = mock_cmd.call_args[0][0]
        assert args == ["send", "--pane", "1", "task", "-s", "my-session"]

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_pane_send_without_session(self, mock_cmd):
        from agentwire.mcp_server import pane_send
        mock_cmd.return_value = _success()
        pane_send(pane=0, message="hi")
        args = mock_cmd.call_args[0][0]
        assert args == ["send", "--pane", "0", "hi"]

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_pane_output_success(self, mock_cmd):
        from agentwire.mcp_server import pane_output
        mock_cmd.return_value = _success(output="some output")
        result = pane_output(pane=1)
        assert result == "some output"

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_pane_output_failure(self, mock_cmd):
        from agentwire.mcp_server import pane_output
        mock_cmd.return_value = _failure("pane not found")
        result = pane_output(pane=99)
        assert "Failed" in result


# ---------------------------------------------------------------------------
# Voice tools
# ---------------------------------------------------------------------------


class TestVoiceTools:
    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_notify_with_target(self, mock_cmd):
        from agentwire.mcp_server import notify
        mock_cmd.return_value = _success()
        notify(text="hey", to="main")
        args = mock_cmd.call_args[0][0]
        assert args == ["notify-parent", "--to", "main", "hey"]

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_notify_without_target(self, mock_cmd):
        from agentwire.mcp_server import notify
        mock_cmd.return_value = _success()
        notify(text="hey")
        args = mock_cmd.call_args[0][0]
        assert args == ["notify-parent", "hey"]

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_notify_failure(self, mock_cmd):
        from agentwire.mcp_server import notify
        mock_cmd.return_value = _failure("no portal")
        result = notify(text="hey")
        assert "Failed" in result


# ---------------------------------------------------------------------------
# Desktop tools
# ---------------------------------------------------------------------------


class TestDesktopTools:
    @patch("agentwire.mcp_server._portal_request")
    def test_windows_list_empty(self, mock_req):
        from agentwire.mcp_server import desktop_windows_list
        mock_req.return_value = {"success": True, "windows": []}
        result = desktop_windows_list()
        assert "No windows" in result

    @patch("agentwire.mcp_server._portal_request")
    def test_open_session(self, mock_req):
        from agentwire.mcp_server import desktop_open_session
        mock_req.return_value = {"success": True, "window_id": "win-1"}
        result = desktop_open_session(session="app", mode="monitor")
        mock_req.assert_called_once_with("POST", "/api/desktop/window/open", {
            "type": "session", "session": "app", "mode": "monitor",
        })
        assert "win-1" in result

    @patch("agentwire.mcp_server._portal_request")
    def test_write_artifact_success(self, mock_req):
        from agentwire.mcp_server import desktop_write_artifact
        mock_req.side_effect = [
            {"success": True, "path": "/tmp/x.html", "url": "/artifacts/x.html"},
            {"success": True, "window_id": "art-1"},
        ]
        result = desktop_write_artifact(filename="x.html", html_content="<h1>Hi</h1>")
        assert "art-1" in result
        assert mock_req.call_count == 2

    @patch("agentwire.mcp_server._portal_request")
    def test_write_artifact_upload_failure(self, mock_req):
        from agentwire.mcp_server import desktop_write_artifact
        mock_req.return_value = {"success": False, "error": "too large"}
        result = desktop_write_artifact(filename="x.html", html_content="data")
        assert "Failed" in result

    @patch("agentwire.mcp_server._portal_request")
    def test_close_window(self, mock_req):
        from agentwire.mcp_server import desktop_close_window
        mock_req.return_value = {"success": True}
        result = desktop_close_window(window_id="win-1")
        assert "closed" in result


# ---------------------------------------------------------------------------
# Scheduler tools
# ---------------------------------------------------------------------------


class TestSchedulerTools:
    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_scheduler_status(self, mock_cmd):
        from agentwire.mcp_server import scheduler_status
        mock_cmd.return_value = _success(
            running=True, task_count=5, enabled_count=3,
            next_task="daily-check", next_in_seconds=120,
        )
        result = scheduler_status()
        assert "running" in result
        assert "3/5" in result
        assert "daily-check" in result

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_task_run_success(self, mock_cmd):
        from agentwire.mcp_server import task_run
        mock_cmd.return_value = _success(
            status="complete", summary="All good", attempt=1,
        )
        result = task_run(session="app", task="daily")
        assert "complete" in result
        args = mock_cmd.call_args[0][0]
        assert args == ["ensure", "-s", "app", "--task", "daily"]

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_task_run_exit_code_3(self, mock_cmd):
        from agentwire.mcp_server import task_run
        mock_cmd.return_value = {"success": False, "error": "locked", "exit_code": 3}
        result = task_run(session="app", task="x")
        assert "locked" in result.lower()


# ---------------------------------------------------------------------------
# History tools
# ---------------------------------------------------------------------------


class TestHistoryTools:
    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_history_list(self, mock_cmd):
        from agentwire.mcp_server import history_list
        mock_cmd.return_value = _success(items=[
            {"sessionId": "abc123", "firstMessage": "fix bug", "messageCount": 5},
        ])
        result = history_list()
        assert "abc123" in result
        assert "fix bug" in result

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_history_show(self, mock_cmd):
        from agentwire.mcp_server import history_show
        mock_cmd.return_value = _success(
            sessionId="abc123", firstMessage="fix bug",
            gitBranch="main", messageCount=10,
        )
        result = history_show(session_id="abc123")
        assert "abc123" in result
        assert "main" in result


# ---------------------------------------------------------------------------
# Email tool
# ---------------------------------------------------------------------------


class TestEmailTool:
    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_email_send_full(self, mock_cmd):
        from agentwire.mcp_server import email_send
        mock_cmd.return_value = _success()
        result = email_send(body="hi", to="a@b.com", subject="test")
        args = mock_cmd.call_args[0][0]
        assert args == ["email", "--body", "hi", "--to", "a@b.com", "--subject", "test"]
        assert "sent" in result.lower()

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_email_send_minimal(self, mock_cmd):
        from agentwire.mcp_server import email_send
        mock_cmd.return_value = _success()
        email_send(body="content only")
        args = mock_cmd.call_args[0][0]
        assert args == ["email", "--body", "content only"]

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_email_send_with_attachments(self, mock_cmd):
        from agentwire.mcp_server import email_send
        mock_cmd.return_value = _success()
        email_send(body="see attached", attachments=["/tmp/a.pdf", "/tmp/b.csv"])
        args = mock_cmd.call_args[0][0]
        assert "--attach" in args
        assert args.count("--attach") == 2
        assert "/tmp/a.pdf" in args
        assert "/tmp/b.csv" in args

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_email_send_plain_text(self, mock_cmd):
        from agentwire.mcp_server import email_send
        mock_cmd.return_value = _success()
        email_send(body="plain msg", plain_text=True)
        args = mock_cmd.call_args[0][0]
        assert "--plain" in args

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_email_send_plain_text_false(self, mock_cmd):
        from agentwire.mcp_server import email_send
        mock_cmd.return_value = _success()
        email_send(body="html msg", plain_text=False)
        args = mock_cmd.call_args[0][0]
        assert "--plain" not in args


# ---------------------------------------------------------------------------
# Scheduler enable/disable/history tools
# ---------------------------------------------------------------------------


class TestSchedulerEnableDisable:
    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_scheduler_enable(self, mock_cmd):
        from agentwire.mcp_server import scheduler_enable
        mock_cmd.return_value = _success()
        result = scheduler_enable(task="daily-check")
        mock_cmd.assert_called_once_with(
            ["scheduler", "enable", "daily-check"], json_output=False
        )
        assert "enabled" in result.lower()

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_scheduler_enable_failure(self, mock_cmd):
        from agentwire.mcp_server import scheduler_enable
        mock_cmd.return_value = _failure("Task 'nope' not found")
        result = scheduler_enable(task="nope")
        assert "Failed" in result

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_scheduler_disable(self, mock_cmd):
        from agentwire.mcp_server import scheduler_disable
        mock_cmd.return_value = _success()
        result = scheduler_disable(task="daily-check")
        mock_cmd.assert_called_once_with(
            ["scheduler", "disable", "daily-check"], json_output=False
        )
        assert "disabled" in result.lower()

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_scheduler_disable_failure(self, mock_cmd):
        from agentwire.mcp_server import scheduler_disable
        mock_cmd.return_value = _failure("Task 'nope' not found")
        result = scheduler_disable(task="nope")
        assert "Failed" in result


class TestSchedulerHistory:
    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_scheduler_history_success(self, mock_cmd):
        from agentwire.mcp_server import scheduler_history
        mock_cmd.return_value = _success(history=[
            {"task": "code-quality", "last_run": "2026-02-20T10:00:00",
             "last_status": "complete", "last_duration": 120, "run_count": 5},
            {"task": "doc-drift", "last_run": "2026-02-20T08:00:00",
             "last_status": "complete", "last_duration": 60, "run_count": 3},
        ])
        result = scheduler_history()
        assert "code-quality" in result
        assert "doc-drift" in result
        assert "complete" in result
        mock_cmd.assert_called_once_with(["scheduler", "history", "--json"])

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_scheduler_history_empty(self, mock_cmd):
        from agentwire.mcp_server import scheduler_history
        mock_cmd.return_value = _success(history=[])
        result = scheduler_history()
        assert "No run history" in result

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_scheduler_history_failure(self, mock_cmd):
        from agentwire.mcp_server import scheduler_history
        mock_cmd.return_value = _failure("board not found")
        result = scheduler_history()
        assert "Failed" in result

    @patch("agentwire.mcp_server.run_agentwire_cmd")
    def test_scheduler_history_limit(self, mock_cmd):
        from agentwire.mcp_server import scheduler_history
        mock_cmd.return_value = _success(history=[
            {"task": f"task-{i}", "last_run": f"2026-02-20T{10-i:02d}:00:00",
             "last_status": "complete", "last_duration": 60, "run_count": 1}
            for i in range(10)
        ])
        result = scheduler_history(limit=3)
        # Should only show 3 most recent
        lines = [l for l in result.split("\n") if l.startswith("  ")]
        assert len(lines) == 3
