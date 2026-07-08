"""`_guarded_launch_command` (#739, #743) — the pane's cd+agent-launch line
must guard a missing worktree dir instead of crashing the agent into a bare
shell nobody reaps, and (#743) route the alert to a real parent — not just
the owner's email — when one is recorded in the launch env."""

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
        assert cmd == (
            'cd /tmp/wt || { echo "agentwire: worktree missing at launch, '
            'aborting: /tmp/wt" >&2; [ -n "$AGENTWIRE_CREATED_BY" ] && agentwire '
            'msg send --to "$AGENTWIRE_CREATED_BY" --kind escalation --subject '
            '"agentwire: worktree missing at launch — '
            '${AGENTWIRE_SESSION_NAME:-unknown session}" --body "cd failed at '
            'launch: /tmp/wt" >/dev/null 2>&1; [ -z "$AGENTWIRE_CREATED_BY" ] && '
            '[ "$AGENTWIRE_UNATTENDED" = "1" ] && agentwire email --subject '
            '"agentwire: worktree missing — '
            '${AGENTWIRE_SESSION_NAME:-unknown session}" --body "cd failed at '
            'launch: /tmp/wt" >/dev/null 2>&1; exit 1; }'
        )

    def test_path_with_spaces_is_quoted(self):
        cmd = _guarded_launch_command("/tmp/my wt", "claude")
        assert "cd '/tmp/my wt'" in cmd

    def test_alert_guarded_on_unattended_env_var(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude")
        assert '[ "$AGENTWIRE_UNATTENDED" = "1" ] && agentwire email' in cmd


class TestParentEscalation:
    """#743: a real parent (`$AGENTWIRE_CREATED_BY`, stamped by
    `_set_session_name_env` only for a non-root session) gets the crash
    routed to its msg inbox; the owner-email fallback still fires only when
    there's no parent."""

    def test_parent_notify_clause_present(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude")
        assert (
            '[ -n "$AGENTWIRE_CREATED_BY" ] && agentwire msg send '
            '--to "$AGENTWIRE_CREATED_BY" --kind escalation'
        ) in cmd

    def test_email_fallback_branch_still_present(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude")
        assert (
            '[ -z "$AGENTWIRE_CREATED_BY" ] && [ "$AGENTWIRE_UNATTENDED" = "1" ] '
            '&& agentwire email'
        ) in cmd

    def test_path_still_quoted_with_parent_escalation_present(self):
        cmd = _guarded_launch_command("/tmp/my wt", "claude")
        assert "cd '/tmp/my wt'" in cmd
        assert '$AGENTWIRE_CREATED_BY' in cmd

    def test_agent_still_gated_behind_successful_cd(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude --flag")
        assert cmd.endswith("; exit 1; } && claude --flag")
