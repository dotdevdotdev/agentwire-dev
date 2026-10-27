"""`_launch_tmux_session` must not TYPE the launch line into the pane (#856).

`send-keys` fires 0.1s after `tmux new-session`, before the shell has put its
tty into raw mode, so the keystrokes land in the tty's canonical-mode input
buffer — capped at 1024 bytes per line on macOS. Past the cap the tail is
discarded silently, and a launch line ending in
`--append-system-prompt "$(</tmp/…)"` truncates into something syntactically
incomplete: zsh waits at a continuation prompt forever and `claude` never
runs. The fix carries the line in as an env var (`tmux -e`, not keyboard
input) and types a fixed-length `eval` instead.
"""

from unittest.mock import patch

from agentwire.core import (
    LAUNCH_CMD_ENV,
    _launch_tmux_session,
)

# macOS `MAX_CANON` / `N_TTY_BUF_SIZE`: the per-line cap on canonical-mode tty
# input. The whole point of the fix is that the typed line stays far under it
# no matter how long the real launch command gets.
TTY_CANON_LIMIT = 1024


def _local_calls(session="proj/branch", path="/tmp/wt", env=None, agent="claude --flag"):
    """Run a local launch with tmux mocked; return the two subprocess argvs."""
    with patch("agentwire.core.subprocess.run") as run, patch("time.sleep"):
        _launch_tmux_session(session, path, dict(env or {}), agent)
    return [call.args[0] for call in run.call_args_list]


class TestLaunchCommandTravelsAsEnv:
    def test_send_keys_payload_is_short_and_fixed_length(self):
        """The typed line's length must not depend on the launch command."""
        short = _local_calls(path="/tmp/a", agent="claude")[1]
        long_path = "/Users/dotdev/projects/agentwire-dev-worktrees/" + "x" * 300
        long_cmd = 'claude --dangerously-skip-permissions --append-system-prompt "$(</var/folders/xx/yy/T/tmp1234.txt)"'
        long = _local_calls(path=long_path, agent=long_cmd)[1]

        typed_short = short[short.index("send-keys") + 4]
        typed_long = long[long.index("send-keys") + 4]
        assert typed_short == typed_long
        assert len(typed_long) < 200


class TestRemoteLaunch:
    def test_remote_send_keys_is_short_and_env_carries_the_command(self):
        path = (
            "/Users/dotdev/projects/agentwire-dev-worktrees/"
            "scheduler-ai-morning-briefing-20260803-100520"
        )
        agent = (
            "claude --dangerously-skip-permissions --append-system-prompt "
            '"$(</var/folders/1d/g63f4vld5x79q6m_swhxjpb0000gn/T/tmpabcd1234.txt)"'
        )
        with patch("agentwire.core._run_remote") as remote:
            _launch_tmux_session("s", path, {}, agent, machine_id="box")
        composite = remote.call_args.args[1]

        assert LAUNCH_CMD_ENV in composite
        # The send-keys half of the composite must stay under the tty cap even
        # though the whole SSH command (which carries the -e flag) is longer.
        send_half = composite.split("tmux send-keys", 1)[1]
        assert len(send_half) < TTY_CANON_LIMIT
