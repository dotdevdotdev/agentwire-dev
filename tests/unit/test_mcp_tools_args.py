"""Integration tests for MCP tools — verify CLI args construction and result formatting."""

from unittest.mock import patch


def _success(**extra):
    return {"success": True, **extra}


# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------


class TestSessionTools:
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
        assert args == [
            "new",
            "-s",
            "test",
            "-p",
            "/other/project",
            "--caller-session",
            "orchestrator",
        ]
        assert "--created-by" not in args

    @patch("agentwire.mcp_session.run_agentwire_cmd")
    @patch("agentwire.mcp_session.get_caller_session", return_value="orchestrator")
    def test_session_create_created_by_wins_over_standalone(self, _caller, mock_cmd):
        # #712 — an explicit created_by beats standalone: the caller asked for
        # a specific parent, so honor it rather than forcing a root.
        from agentwire.mcp_session import session_create

        mock_cmd.return_value = _success()
        session_create(name="test", project_dir="/p", created_by="orchestrator", standalone=True)
        args = mock_cmd.call_args[0][0]
        assert args == ["new", "-s", "test", "-p", "/p", "--created-by", "orchestrator"]
        assert "--caller-session" not in args


# ---------------------------------------------------------------------------
# Pane tools
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Voice tools
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Desktop tools
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scheduler tools
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# History tools
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Email tool
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scheduler enable/disable/history tools
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Worktree tools
# ---------------------------------------------------------------------------


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
        assert args == ["worktree", "x", "-p", "/other/project", "--caller-session", "orchestrator"]
        assert "--created-by" not in args
