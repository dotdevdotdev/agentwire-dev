"""Integration tests for MCP tools — verify CLI args construction and result formatting."""

from unittest.mock import patch


def _success(**extra):
    return {"success": True, **extra}


def _failure(error="something broke"):
    return {"success": False, "error": error}


# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------


class TestSessionTools:
    @patch("agentwire.mcp_session.run_agentwire_cmd")
    def test_sessions_list_success(self, mock_cmd):
        from agentwire.mcp_session import sessions_list
        mock_cmd.return_value = _success(sessions=[
            {"name": "app", "machine": "local", "windows": 1, "path": "/p", "posture": "bypass"},
        ])
        result = sessions_list()
        mock_cmd.assert_called_once_with(["list", "--sessions"])
        assert "app" in result

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    def test_sessions_list_failure(self, mock_cmd):
        from agentwire.mcp_session import sessions_list
        mock_cmd.return_value = _failure("tmux not running")
        result = sessions_list()
        assert "Failed" in result

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value=None)
    def test_session_create_minimal(self, _caller, mock_cmd):
        from agentwire.mcp_session import session_create
        mock_cmd.return_value = _success()
        result = session_create(name="test")
        mock_cmd.assert_called_once_with(["new", "-s", "test"])
        assert "created" in result

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value=None)
    def test_session_create_all_args(self, _caller, mock_cmd):
        from agentwire.mcp_session import session_create
        mock_cmd.return_value = _success()
        session_create(name="x", project_dir="/p", roles="voice,worker", posture="bare")
        args = mock_cmd.call_args[0][0]
        assert args == ["new", "-s", "x", "-p", "/p", "--roles", "voice,worker", "--posture", "bare"]

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value="orchestrator")
    def test_session_create_forwards_caller_as_candidate(self, _caller, mock_cmd):
        # #715 — the caller is forwarded as a CANDIDATE (--caller-session), not
        # a forced --created-by; cmd_new decides whether to inherit it based
        # on whether project_dir is the caller's own project.
        from agentwire.mcp_session import session_create
        mock_cmd.return_value = _success()
        session_create(name="test", project_dir="/other/project")
        args = mock_cmd.call_args[0][0]
        assert args == ["new", "-s", "test", "-p", "/other/project",
                         "--caller-session", "orchestrator"]
        assert "--created-by" not in args

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value=None)
    def test_session_create_no_caller_forwards_nothing(self, _caller, mock_cmd):
        from agentwire.mcp_session import session_create
        mock_cmd.return_value = _success()
        session_create(name="test")
        args = mock_cmd.call_args[0][0]
        assert "--caller-session" not in args

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value="orchestrator")
    def test_session_create_explicit_created_by_overrides_and_skips_caller_session(
            self, _caller, mock_cmd):
        # Parity with worktree_create's created_by override (#715) — forces a
        # specific parent for the related-project case instead of relying on
        # the default same-project-conditional inheritance.
        from agentwire.mcp_session import session_create
        mock_cmd.return_value = _success()
        session_create(name="test", project_dir="/other/project", created_by="orchestrator")
        args = mock_cmd.call_args[0][0]
        assert args == ["new", "-s", "test", "-p", "/other/project",
                         "--created-by", "orchestrator"]
        assert "--caller-session" not in args

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value="orchestrator")
    def test_session_create_standalone_forces_empty_created_by(self, _caller, mock_cmd):
        # #712 — standalone=True forces `--created-by ''` (an explicit empty
        # string the CLI reads as "opt out of inheritance") even in the
        # caller's own project, and skips the default --caller-session
        # candidate-forwarding entirely.
        from agentwire.mcp_session import session_create
        mock_cmd.return_value = _success()
        session_create(name="test", project_dir="/p", standalone=True)
        args = mock_cmd.call_args[0][0]
        assert args == ["new", "-s", "test", "-p", "/p", "--created-by", ""]
        assert "--caller-session" not in args

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value="orchestrator")
    def test_session_create_created_by_wins_over_standalone(self, _caller, mock_cmd):
        # #712 — an explicit created_by beats standalone: the caller asked for
        # a specific parent, so honor it rather than forcing a root.
        from agentwire.mcp_session import session_create
        mock_cmd.return_value = _success()
        session_create(name="test", project_dir="/p", created_by="orchestrator",
                       standalone=True)
        args = mock_cmd.call_args[0][0]
        assert args == ["new", "-s", "test", "-p", "/p",
                         "--created-by", "orchestrator"]
        assert "--caller-session" not in args

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value="orchestrator")
    def test_session_send_cross_session(self, mock_caller, mock_cmd):
        from agentwire.mcp_session import session_send
        mock_cmd.return_value = _success(verified=True)
        session_send(session="worker", message="do task")
        args = mock_cmd.call_args[0][0]
        assert args[:4] == ["send", "-s", "worker", "--verify"]
        sent_msg = args[4]  # message follows --verify
        assert '[MESSAGE FROM SESSION "orchestrator"' in sent_msg
        assert 'session_send(session="orchestrator"' in sent_msg
        # #835 review: a msg-inbox fallback attributes to the real caller
        # (dead-letter emails, the rendered header) instead of the generic
        # "agentwire" -- the CLI subprocess can't auto-detect it itself.
        assert args[5:] == ["--caller-session", "orchestrator"]

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value=None)
    def test_session_send_no_caller(self, mock_caller, mock_cmd):
        from agentwire.mcp_session import session_send
        mock_cmd.return_value = _success(verified=True)
        result = session_send(session="target", message="hello")
        args = mock_cmd.call_args[0][0]
        assert args == ["send", "-s", "target", "--verify", "hello"]
        assert "verified" in result.lower()


# ---------------------------------------------------------------------------
# Pane tools
# ---------------------------------------------------------------------------


class TestPaneTools:
    @patch("agentwire.mcp_pane.run_agentwire_cmd")
    def test_pane_send_with_session(self, mock_cmd):
        from agentwire.mcp_pane import pane_send
        mock_cmd.return_value = _success(verified=True)
        pane_send(pane=1, message="task", session="my-session")
        args = mock_cmd.call_args[0][0]
        assert args == ["send", "--pane", "1", "--verify", "task", "-s", "my-session"]

    @patch("agentwire.mcp_pane.run_agentwire_cmd")
    def test_pane_send_without_session(self, mock_cmd):
        from agentwire.mcp_pane import pane_send
        mock_cmd.return_value = _success(verified=True)
        pane_send(pane=0, message="hi")
        args = mock_cmd.call_args[0][0]
        assert args == ["send", "--pane", "0", "--verify", "hi"]

    @patch("agentwire.mcp_pane.run_agentwire_cmd")
    def test_pane_output_success(self, mock_cmd):
        from agentwire.mcp_pane import pane_output
        mock_cmd.return_value = _success(output="some output")
        result = pane_output(pane=1)
        assert result == "some output"

    @patch("agentwire.mcp_pane.run_agentwire_cmd")
    def test_pane_output_failure(self, mock_cmd):
        from agentwire.mcp_pane import pane_output
        mock_cmd.return_value = _failure("pane not found")
        result = pane_output(pane=99)
        assert "Failed" in result


# ---------------------------------------------------------------------------
# Voice tools
# ---------------------------------------------------------------------------


class TestVoiceTools:
    @patch("agentwire.mcp_notify.run_agentwire_cmd")
    def test_notify_with_target(self, mock_cmd):
        from agentwire.mcp_notify import notify_parent
        mock_cmd.return_value = _success(delivered=True, target="main")
        result = notify_parent(text="hey", session="main")
        args = mock_cmd.call_args[0][0]
        assert args == ["notify-parent", "--to", "main", "hey"]
        assert "delivered" in result.lower()

    @patch("agentwire.mcp_notify.run_agentwire_cmd")
    def test_notify_without_target(self, mock_cmd):
        from agentwire.mcp_notify import notify_parent
        mock_cmd.return_value = _success(delivered=True, target="parent")
        notify_parent(text="hey")
        args = mock_cmd.call_args[0][0]
        assert args == ["notify-parent", "hey"]

    @patch("agentwire.mcp_notify.run_agentwire_cmd")
    def test_notify_not_delivered(self, mock_cmd):
        from agentwire.mcp_notify import notify_parent
        # safe_deliver refused (e.g. parked session) — surfaced, not hidden.
        mock_cmd.return_value = {"success": False, "delivered": False,
                                 "target": "main", "reason": "session is parked"}
        result = notify_parent(text="hey", session="main")
        assert "NOT delivered" in result
        assert "parked" in result


def _toast_response(**fields):
    """A 200 from the portal's notification endpoint, as `requests` returns it."""
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "id": "n1", "clients": 1, **fields}

    return _Response()


class TestNotifyUserArtifactParams:
    """#822: notify_user's artifact_url/artifact_title params (#821) had zero
    test coverage — cover the body it builds for /api/desktop/notification."""

    # Patched at core's ONE portal call, not at a transport inside mcp_notify:
    # since #1016 every toast producer (CLI, MCP, `say --display`) goes through
    # `core.post_desktop_notification`, so this asserts the body that actually
    # reaches /api/desktop/notification.

    @patch("agentwire.core.portal_request")
    def test_artifact_url_sets_artifact_body_with_default_title(self, mock_req):
        from agentwire.mcp_notify import notify_user
        mock_req.return_value = _toast_response()
        notify_user(text="ready", artifact_url="report.html")
        body = mock_req.call_args.kwargs["json"]
        assert body["artifact"] == {"url": "report.html", "title": "Artifact"}

    @patch("agentwire.core.portal_request")
    def test_artifact_title_overrides_default(self, mock_req):
        from agentwire.mcp_notify import notify_user
        mock_req.return_value = _toast_response()
        notify_user(text="ready", artifact_url="report.html", artifact_title="Q3 Report")
        body = mock_req.call_args.kwargs["json"]
        assert body["artifact"] == {"url": "report.html", "title": "Q3 Report"}

    @patch("agentwire.core.portal_request")
    def test_no_artifact_url_omits_artifact_body(self, mock_req):
        from agentwire.mcp_notify import notify_user
        mock_req.return_value = _toast_response()
        notify_user(text="plain toast", artifact_title="ignored without a url")
        body = mock_req.call_args.kwargs["json"]
        assert "artifact" not in body


# ---------------------------------------------------------------------------
# Desktop tools
# ---------------------------------------------------------------------------


class TestDesktopTools:
    @patch("agentwire.mcp_desktop._portal_request")
    def test_windows_list_empty(self, mock_req):
        from agentwire.mcp_desktop import desktop_windows_list
        mock_req.return_value = {"success": True, "windows": []}
        result = desktop_windows_list()
        assert "No windows" in result

    @patch("agentwire.mcp_desktop._portal_request")
    def test_open_session(self, mock_req):
        from agentwire.mcp_desktop import desktop_open_session
        mock_req.return_value = {"success": True, "window_id": "win-1"}
        result = desktop_open_session(session="app", mode="monitor")
        mock_req.assert_called_once_with("POST", "/api/desktop/window/open", {
            "type": "session", "session": "app", "mode": "monitor",
        })
        assert "win-1" in result

    @patch("agentwire.mcp_desktop._portal_request")
    def test_write_artifact_success(self, mock_req):
        from agentwire.mcp_desktop import desktop_write_artifact
        mock_req.side_effect = [
            {"success": True, "path": "/tmp/x.html", "url": "/artifacts/x.html"},
            {"success": True, "id": "toast-1234", "clients": 1},
        ]
        result = desktop_write_artifact(filename="x.html", html_content="<h1>Hi</h1>")
        assert "toast-1234" in result
        assert "announced" in result
        assert mock_req.call_count == 2
        # Step 2 is the click-to-open notification (#817), never a window open.
        assert mock_req.call_args_list[1].args == (
            "POST", "/api/desktop/notification",
            {"artifact": {"url": "x.html", "title": "Artifact"}},
        )

    @patch("agentwire.mcp_desktop._portal_request")
    def test_open_artifact_announces_not_opens(self, mock_req):
        from agentwire.mcp_desktop import desktop_open_artifact
        mock_req.return_value = {"success": True, "id": "toast-5678", "clients": 0}
        result = desktop_open_artifact(url="report.html", title="Report", artifact_id="rep-1")
        mock_req.assert_called_once_with("POST", "/api/desktop/notification", {
            "artifact": {"url": "report.html", "title": "Report", "artifact_id": "rep-1"},
        })
        assert "toast-5678" in result

    @patch("agentwire.mcp_desktop._portal_request")
    def test_write_artifact_upload_failure(self, mock_req):
        from agentwire.mcp_desktop import desktop_write_artifact
        mock_req.return_value = {"success": False, "error": "too large"}
        result = desktop_write_artifact(filename="x.html", html_content="data")
        assert "Failed" in result

    @patch("agentwire.mcp_desktop._portal_request")
    def test_close_window(self, mock_req):
        from agentwire.mcp_desktop import desktop_close_window
        mock_req.return_value = {"success": True}
        result = desktop_close_window(window_id="win-1")
        assert "closed" in result


# ---------------------------------------------------------------------------
# Scheduler tools
# ---------------------------------------------------------------------------


class TestSchedulerTools:
    @patch("agentwire.mcp_scheduler.run_agentwire_cmd")
    def test_scheduler_status(self, mock_cmd):
        from agentwire.mcp_scheduler import scheduler_status
        mock_cmd.return_value = _success(
            running=True, task_count=5, enabled_count=3,
            next_task="daily-check", next_in_seconds=120,
        )
        result = scheduler_status()
        assert "running" in result
        assert "3/5" in result
        assert "daily-check" in result

    @patch("agentwire.mcp_task.run_agentwire_cmd")
    def test_task_run_success(self, mock_cmd):
        from agentwire.mcp_task import task_run
        mock_cmd.return_value = _success(
            status="complete", summary="All good", attempt=1,
        )
        result = task_run(session="app", task="daily")
        assert "complete" in result
        args = mock_cmd.call_args[0][0]
        assert args == ["ensure", "-s", "app", "--task", "daily"]

    @patch("agentwire.mcp_task.run_agentwire_cmd")
    def test_task_run_exit_code_3(self, mock_cmd):
        from agentwire.mcp_task import task_run
        mock_cmd.return_value = {"success": False, "error": "locked", "exit_code": 3}
        result = task_run(session="app", task="x")
        assert "locked" in result.lower()


# ---------------------------------------------------------------------------
# History tools
# ---------------------------------------------------------------------------


class TestHistoryTools:
    @patch("agentwire.mcp_history.run_agentwire_cmd")
    def test_history_list(self, mock_cmd):
        from agentwire.mcp_history import history_list
        mock_cmd.return_value = _success(items=[
            {"sessionId": "abc123", "firstMessage": "fix bug", "messageCount": 5},
        ])
        result = history_list()
        assert "abc123" in result
        assert "fix bug" in result

    @patch("agentwire.mcp_history.run_agentwire_cmd")
    def test_history_show(self, mock_cmd):
        from agentwire.mcp_history import history_show
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
    @patch("agentwire.mcp_channels.run_agentwire_cmd")
    def test_email_send_full(self, mock_cmd):
        from agentwire.mcp_channels import email_send
        mock_cmd.return_value = _success()
        result = email_send(body="hi", to="a@b.com", subject="test")
        args = mock_cmd.call_args[0][0]
        assert args == ["email", "--body", "hi", "--to", "a@b.com", "--subject", "test"]
        # Honest async boundary: accepted by provider, not "delivered" (#444).
        assert "accepted by provider" in result.lower()

    @patch("agentwire.mcp_channels.run_agentwire_cmd")
    def test_email_send_minimal(self, mock_cmd):
        from agentwire.mcp_channels import email_send
        mock_cmd.return_value = _success()
        email_send(body="content only")
        args = mock_cmd.call_args[0][0]
        assert args == ["email", "--body", "content only"]

    @patch("agentwire.mcp_channels.run_agentwire_cmd")
    def test_email_send_with_attachments(self, mock_cmd):
        from agentwire.mcp_channels import email_send
        mock_cmd.return_value = _success()
        email_send(body="see attached", attachments=["/tmp/a.pdf", "/tmp/b.csv"])
        args = mock_cmd.call_args[0][0]
        assert "--attach" in args
        assert args.count("--attach") == 2
        assert "/tmp/a.pdf" in args
        assert "/tmp/b.csv" in args

    @patch("agentwire.mcp_channels.run_agentwire_cmd")
    def test_email_send_plain_text(self, mock_cmd):
        from agentwire.mcp_channels import email_send
        mock_cmd.return_value = _success()
        email_send(body="plain msg", plain_text=True)
        args = mock_cmd.call_args[0][0]
        assert "--plain" in args

    @patch("agentwire.mcp_channels.run_agentwire_cmd")
    def test_email_send_plain_text_false(self, mock_cmd):
        from agentwire.mcp_channels import email_send
        mock_cmd.return_value = _success()
        email_send(body="html msg", plain_text=False)
        args = mock_cmd.call_args[0][0]
        assert "--plain" not in args


# ---------------------------------------------------------------------------
# Scheduler enable/disable/history tools
# ---------------------------------------------------------------------------


class TestSchedulerEnableDisable:
    @patch("agentwire.mcp_scheduler.run_agentwire_cmd")
    def test_scheduler_enable(self, mock_cmd):
        from agentwire.mcp_scheduler import scheduler_enable
        mock_cmd.return_value = _success()
        result = scheduler_enable(task="daily-check")
        mock_cmd.assert_called_once_with(
            ["scheduler", "enable", "daily-check"], json_output=False
        )
        assert "enabled" in result.lower()

    @patch("agentwire.mcp_scheduler.run_agentwire_cmd")
    def test_scheduler_enable_failure(self, mock_cmd):
        from agentwire.mcp_scheduler import scheduler_enable
        mock_cmd.return_value = _failure("Task 'nope' not found")
        result = scheduler_enable(task="nope")
        assert "Failed" in result

    @patch("agentwire.mcp_scheduler.run_agentwire_cmd")
    def test_scheduler_disable(self, mock_cmd):
        from agentwire.mcp_scheduler import scheduler_disable
        mock_cmd.return_value = _success()
        result = scheduler_disable(task="daily-check")
        mock_cmd.assert_called_once_with(
            ["scheduler", "disable", "daily-check"], json_output=False
        )
        assert "disabled" in result.lower()

    @patch("agentwire.mcp_scheduler.run_agentwire_cmd")
    def test_scheduler_disable_failure(self, mock_cmd):
        from agentwire.mcp_scheduler import scheduler_disable
        mock_cmd.return_value = _failure("Task 'nope' not found")
        result = scheduler_disable(task="nope")
        assert "Failed" in result


class TestSchedulerHistory:
    @patch("agentwire.mcp_scheduler.run_agentwire_cmd")
    def test_scheduler_history_success(self, mock_cmd):
        from agentwire.mcp_scheduler import scheduler_history
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

    @patch("agentwire.mcp_scheduler.run_agentwire_cmd")
    def test_scheduler_history_empty(self, mock_cmd):
        from agentwire.mcp_scheduler import scheduler_history
        mock_cmd.return_value = _success(history=[])
        result = scheduler_history()
        assert "No run history" in result

    @patch("agentwire.mcp_scheduler.run_agentwire_cmd")
    def test_scheduler_history_failure(self, mock_cmd):
        from agentwire.mcp_scheduler import scheduler_history
        mock_cmd.return_value = _failure("board not found")
        result = scheduler_history()
        assert "Failed" in result

    @patch("agentwire.mcp_scheduler.run_agentwire_cmd")
    def test_scheduler_history_limit(self, mock_cmd):
        from agentwire.mcp_scheduler import scheduler_history
        mock_cmd.return_value = _success(history=[
            {"task": f"task-{i}", "last_run": f"2026-02-20T{10-i:02d}:00:00",
             "last_status": "complete", "last_duration": 60, "run_count": 1}
            for i in range(10)
        ])
        result = scheduler_history(limit=3)
        # Should only show 3 most recent
        lines = [line for line in result.split("\n") if line.startswith("  ")]
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# Worktree tools
# ---------------------------------------------------------------------------


class TestWorktreeCreateSeedResult:
    """#695 — a failed seed must be LOUD in the tool result. The old contract
    (silently omitting ' (seeded)') left orchestrators believing the task was
    delivered while the session sat idle."""

    def _create(self, mock_cmd, data, prompt="do the task"):
        from agentwire.mcp_worktree import worktree_create
        mock_cmd.return_value = data
        return worktree_create("x", prompt=prompt)

    @patch("agentwire.mcp_worktree.get_caller_session", return_value=None)
    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    def test_seeded_success(self, mock_cmd, _caller):
        result = self._create(mock_cmd, _success(
            session="proj-x", path="/w/proj-x", first_message_delivered=True))
        assert "(seeded)" in result
        assert "WARNING" not in result
        # The failure path (recovery) needs headroom past the 60s boot wait.
        assert mock_cmd.call_args.kwargs["timeout"] >= 180

    @patch("agentwire.mcp_worktree.get_caller_session", return_value=None)
    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    def test_seed_failure_with_inbox_fallback_is_loud(self, mock_cmd, _caller):
        result = self._create(mock_cmd, _success(
            session="proj-x", path="/w/proj-x",
            first_message_delivered=False, first_message_fallback="inbox"))
        assert "WARNING" in result
        assert "NOT delivered" in result
        assert "msg inbox" in result
        assert "(seeded)" not in result

    @patch("agentwire.mcp_worktree.get_caller_session", return_value=None)
    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    def test_seed_failure_with_inbox_stuck_fallback_warns_of_leftover_draft(self, mock_cmd, _caller):
        """#843: "inbox_stuck" (queued, but the stale draft could not be
        confirmed cleared from the input box) must read distinctly from the
        plain "inbox" success-ish case above — the caller needs to know a
        leftover draft may still be sitting in the pane."""
        result = self._create(mock_cmd, _success(
            session="proj-x", path="/w/proj-x",
            first_message_delivered=False, first_message_fallback="inbox_stuck"))
        assert "WARNING" in result
        assert "NOT delivered" in result
        assert "could NOT be confirmed cleared" in result
        assert "(seeded)" not in result

    @patch("agentwire.mcp_worktree.get_caller_session", return_value=None)
    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    def test_seed_failure_without_fallback_is_loud(self, mock_cmd, _caller):
        result = self._create(mock_cmd, _success(
            session="proj-x", path="/w/proj-x",
            first_message_delivered=False, first_message_fallback=None))
        assert "WARNING" in result
        assert "could NOT be queued" in result
        assert "session_send" in result

    @patch("agentwire.mcp_worktree.get_caller_session", return_value=None)
    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    def test_no_prompt_no_seed_talk(self, mock_cmd, _caller):
        result = self._create(
            mock_cmd, _success(session="proj-x", path="/w/proj-x"), prompt="")
        assert "(seeded)" not in result
        assert "WARNING" not in result

    @patch("agentwire.mcp_worktree.get_caller_session", return_value=None)
    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    def test_reattach_with_prompt_notes_undelivered_seed(self, mock_cmd, _caller):
        result = self._create(mock_cmd, _success(
            session="proj-x", path="/w/proj-x", reattached=True))
        assert "Reattached" in result
        assert "NOT" in result  # the seed prompt was not pasted — say so


class TestWorktreeCreateCallerForwarding:
    """#715 — worktree_create must not unconditionally force --created-by to
    the caller anymore (that flattened every cross-project spawn into the
    caller's subtree). It forwards the caller as a --caller-session
    CANDIDATE, letting cmd_new's same-project check decide inheritance,
    unless an explicit `created_by` override is passed."""

    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    @patch("agentwire.mcp_worktree.get_caller_session", return_value="orchestrator")
    def test_default_forwards_caller_session_not_created_by(self, _caller, mock_cmd):
        from agentwire.mcp_worktree import worktree_create
        mock_cmd.return_value = _success(session="proj-x", path="/w/proj-x")
        worktree_create("x", project_dir="/other/project")
        args = mock_cmd.call_args[0][0]
        assert args == ["worktree", "x", "-p", "/other/project",
                         "--caller-session", "orchestrator"]
        assert "--created-by" not in args

    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    @patch("agentwire.mcp_worktree.get_caller_session", return_value=None)
    def test_no_caller_forwards_nothing(self, _caller, mock_cmd):
        from agentwire.mcp_worktree import worktree_create
        mock_cmd.return_value = _success(session="proj-x", path="/w/proj-x")
        worktree_create("x")
        args = mock_cmd.call_args[0][0]
        assert "--caller-session" not in args
        assert "--created-by" not in args

    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    @patch("agentwire.mcp_worktree.get_caller_session", return_value="orchestrator")
    def test_explicit_created_by_overrides_and_skips_caller_session(self, _caller, mock_cmd):
        from agentwire.mcp_worktree import worktree_create
        mock_cmd.return_value = _success(session="proj-x", path="/w/proj-x")
        worktree_create("x", project_dir="/other/project", created_by="orchestrator")
        args = mock_cmd.call_args[0][0]
        assert args == ["worktree", "x", "-p", "/other/project",
                         "--created-by", "orchestrator"]
        assert "--caller-session" not in args

    @patch("agentwire.mcp_worktree.run_agentwire_cmd")
    @patch("agentwire.mcp_worktree.get_caller_session", return_value="orchestrator")
    def test_explicit_empty_created_by_does_not_force_standalone(self, _caller, mock_cmd):
        # KNOWN LIMITATION, not a feature: at the CLI, --created-by '' forces
        # standalone. At the MCP layer, created_by="" is indistinguishable
        # from "omitted" (both are the str="" default), so it falls through
        # to the same default candidate-forwarding as not passing it at all —
        # it does NOT force standalone. There is currently no way to force
        # standalone through this MCP tool; only the raw CLI's
        # `agentwire worktree --created-by ''` does. A dedicated boolean
        # (mirroring #712/#713's `standalone` param on session_create) would
        # be the correct fix, not overloading this string param further.
        from agentwire.mcp_worktree import worktree_create
        mock_cmd.return_value = _success(session="proj-x", path="/w/proj-x")
        worktree_create("x", project_dir="/other/project", created_by="")
        args = mock_cmd.call_args[0][0]
        assert args == ["worktree", "x", "-p", "/other/project",
                         "--caller-session", "orchestrator"]
