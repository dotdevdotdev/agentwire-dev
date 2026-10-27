"""`_guarded_launch_command` (#739, #743) — the pane's cd+agent-launch line
must guard a missing worktree dir instead of crashing the agent into a bare
shell nobody reaps, and (#743) route the alert to a real parent — not just
the owner's email — when one is recorded in the launch env."""

from agentwire.core import _guarded_launch_command


class TestGuardedLaunchCommand:
    def test_cd_failure_exits_without_running_agent(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude --flag")
        # The guard clause exits the shell on cd failure — the agent segment
        # is only reachable via the `&&` after a successful cd.
        assert "exit 1" in cmd
        assert "AGENTWIRE_UNATTENDED" in cmd

    def test_path_with_spaces_is_quoted(self):
        cmd = _guarded_launch_command("/tmp/my wt", "claude")
        assert "cd '/tmp/my wt'" in cmd
