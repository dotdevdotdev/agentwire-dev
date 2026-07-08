"""`_guarded_launch_command` (#739) — the pane's cd+agent-launch line must
guard a missing worktree dir instead of crashing the agent into a bare
shell nobody reaps."""

from agentwire.core import _guarded_launch_command


class TestGuardedLaunchCommand:
    def test_cd_success_runs_agent(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude --flag")
        assert cmd.startswith("cd /tmp/wt || {")
        assert cmd.endswith("&& claude --flag")

    def test_cd_failure_exits_without_running_agent(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude --flag")
        # The guard clause exits the shell on cd failure — the agent segment
        # is only reachable via the `&&` after a successful cd.
        assert "exit 1" in cmd
        assert 'AGENTWIRE_UNATTENDED' in cmd

    def test_bare_posture_has_no_agent_segment(self):
        cmd = _guarded_launch_command("/tmp/wt", None)
        assert cmd == 'cd /tmp/wt || { echo "agentwire: worktree missing at launch, ' \
            'aborting: /tmp/wt" >&2; [ "$AGENTWIRE_UNATTENDED" = "1" ] && agentwire ' \
            'email --subject "agentwire: worktree missing — ' \
            '${AGENTWIRE_SESSION_NAME:-unknown session}" --body "cd failed at ' \
            'launch: /tmp/wt" >/dev/null 2>&1; exit 1; }'

    def test_path_with_spaces_is_quoted(self):
        cmd = _guarded_launch_command("/tmp/my wt", "claude")
        assert "cd '/tmp/my wt'" in cmd

    def test_alert_guarded_on_unattended_env_var(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude")
        assert '[ "$AGENTWIRE_UNATTENDED" = "1" ] && agentwire email' in cmd
