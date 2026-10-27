"""Tests for portal terminal sizing — unpin_tmux_window + no-manual-pin guard (#258).

The portal must never run tmux resize-window: -x/-y pins the window into
manual size mode (defeating the user's window-size policy for every attached
client), and -a/-A resize once but leave manual mode set. The only correct
heal is unsetting the window-level window-size option, which restores the
configured policy and itself triggers a re-fit (verified empirically on
tmux 3.5a).
"""

from unittest.mock import AsyncMock, patch

import pytest

from agentwire.server import unpin_tmux_window


def _mock_subprocess():
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


class TestUnpinTmuxWindow:
    @pytest.mark.asyncio
    async def test_remote_wraps_command_in_ssh(self):
        proc = _mock_subprocess()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock:
            await unpin_tmux_window("myproject/branch", ssh_target="user@host")

        args = exec_mock.call_args.args
        # ssh_base_opts() (ControlMaster multiplexing) is spliced between the
        # "ssh" token and the target, so assert on the stable head/tail.
        assert args[0] == "ssh"
        assert "ControlMaster=auto" in args
        assert args[-2] == "user@host"
        assert args[-1] == "tmux set-option -w -t myproject/branch -u window-size"
        proc.wait.assert_awaited_once()
