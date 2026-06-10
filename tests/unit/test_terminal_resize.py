"""Tests for portal terminal sizing — unpin_tmux_window + no-manual-pin guard (#258).

The portal must never run tmux resize-window: -x/-y pins the window into
manual size mode (defeating the user's window-size policy for every attached
client), and -a/-A resize once but leave manual mode set. The only correct
heal is unsetting the window-level window-size option, which restores the
configured policy and itself triggers a re-fit (verified empirically on
tmux 3.5a).
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentwire.server import unpin_tmux_window

AGENTWIRE_SRC = Path(__file__).parents[2] / "agentwire"


def _mock_subprocess():
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


class TestUnpinTmuxWindow:
    @pytest.mark.asyncio
    async def test_local_unsets_window_size_option(self):
        proc = _mock_subprocess()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock:
            await unpin_tmux_window("myproject")

        args = exec_mock.call_args.args
        assert args == ("tmux", "set-option", "-w", "-t", "myproject", "-u", "window-size")
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remote_wraps_command_in_ssh(self):
        proc = _mock_subprocess()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock:
            await unpin_tmux_window("myproject/branch", ssh_target="user@host")

        args = exec_mock.call_args.args
        assert args[0] == "ssh"
        assert args[1] == "user@host"
        assert args[2] == "tmux set-option -w -t myproject/branch -u window-size"
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remote_quotes_session_names(self):
        proc = _mock_subprocess()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock:
            await unpin_tmux_window("my project", ssh_target="user@host")

        remote_cmd = exec_mock.call_args.args[2]
        assert "'my project'" in remote_cmd


class TestNoManualPinRegression:
    def test_no_resize_window_calls_anywhere(self):
        """tmux resize-window must never be invoked: -x/-y pins manual size
        mode and -a/-A leave it set (#258). Re-fitting is done by unsetting
        the window-size option."""
        for src_file in AGENTWIRE_SRC.rglob("*.py"):
            for lineno, line in enumerate(src_file.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                assert "resize-window" not in code, (
                    f"resize-window invocation found in "
                    f"{src_file.relative_to(AGENTWIRE_SRC)}:{lineno}: {line.strip()!r}"
                )
