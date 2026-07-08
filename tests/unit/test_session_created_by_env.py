"""`_set_session_name_env` stamping `AGENTWIRE_CREATED_BY` (#743).

The bare pre-agent shell's launch-crash guard (`_guarded_launch_command`)
needs a real parent name in the tmux env to route an alert there instead of
just emailing the owner. A `created_by` of '' (root/orchestrator) or None
must NOT set the var at all — its mere presence is the guard's signal that
there's a parent to escalate to.
"""

from agentwire.core import AgentCommand, _build_tmux_env_flags, _set_session_name_env


class TestSetSessionNameEnv:
    def test_session_name_always_stamped(self):
        agent = AgentCommand(command="claude")
        _set_session_name_env(agent, "proj/branch")
        assert agent.env["AGENTWIRE_SESSION_NAME"] == "proj/branch"

    def test_real_parent_sets_created_by(self):
        agent = AgentCommand(command="claude")
        _set_session_name_env(agent, "proj/branch", created_by="orchestrator")
        assert agent.env["AGENTWIRE_CREATED_BY"] == "orchestrator"

    def test_empty_string_created_by_is_not_stamped(self):
        agent = AgentCommand(command="claude")
        _set_session_name_env(agent, "proj/branch", created_by="")
        assert "AGENTWIRE_CREATED_BY" not in agent.env

    def test_none_created_by_is_not_stamped(self):
        agent = AgentCommand(command="claude")
        _set_session_name_env(agent, "proj/branch", created_by=None)
        assert "AGENTWIRE_CREATED_BY" not in agent.env

    def test_default_omits_created_by(self):
        agent = AgentCommand(command="claude")
        _set_session_name_env(agent, "proj/branch")
        assert "AGENTWIRE_CREATED_BY" not in agent.env


class TestEnvReachesTmuxFlags:
    """`tmux new-session -e K=V` is the only path that lands a var in the
    initial pane's shell (post-hoc `set-environment` misses it) — confirm the
    stamped var actually flows into that flag list, not just `agent.env`."""

    def test_created_by_reaches_new_session_flags(self):
        agent = AgentCommand(command="claude")
        _set_session_name_env(agent, "proj/branch", created_by="orchestrator")
        flags = _build_tmux_env_flags(agent.env)
        assert "-e" in flags
        assert "AGENTWIRE_CREATED_BY=orchestrator" in flags

    def test_no_parent_means_no_flag(self):
        agent = AgentCommand(command="claude")
        _set_session_name_env(agent, "proj/branch")
        flags = _build_tmux_env_flags(agent.env)
        assert not any(f.startswith("AGENTWIRE_CREATED_BY=") for f in flags)
