"""`_set_session_name_env` stamping `AGENTWIRE_CREATED_BY` (#743).

The bare pre-agent shell's launch-crash guard (`_guarded_launch_command`)
needs a real parent name in the tmux env to route an alert there instead of
just emailing the owner. A `created_by` of '' (root/orchestrator) or None
must NOT set the var at all — its mere presence is the guard's signal that
there's a parent to escalate to.
"""

from agentwire.core import AgentCommand, _set_session_name_env


class TestSetSessionNameEnv:

    def test_real_parent_sets_created_by(self):
        agent = AgentCommand(command="claude")
        _set_session_name_env(agent, "proj/branch", created_by="orchestrator")
        assert agent.env["AGENTWIRE_CREATED_BY"] == "orchestrator"


