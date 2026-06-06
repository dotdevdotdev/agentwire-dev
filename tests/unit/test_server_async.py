"""Tests for async run_agentwire_cmd in agentwire/server.py."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentwire.config import load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_process(returncode=0, stdout=b"", stderr=b""):
    """Create a mock async subprocess process."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.fixture
def server(tmp_path):
    """Create an AgentWireServer with minimal config."""
    config = load_config(tmp_path / "nonexistent.yaml")
    from agentwire.server import AgentWireServer
    return AgentWireServer(config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAsyncRunAgentwireCmd:
    async def test_success_json(self, server):
        data = {"sessions": [{"name": "test"}]}
        proc = _make_process(stdout=json.dumps(data).encode())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["list", "--sessions"])
        assert success is True
        assert result == data

    async def test_nonzero_returncode(self, server):
        proc = _make_process(returncode=1, stderr=b"session not found")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["kill", "-s", "x"])
        assert success is False
        assert "session not found" in result["error"]

    async def test_json_output_false_success(self, server):
        proc = _make_process(stdout=b"raw output text")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["say", "hello"], json_output=False)
        assert success is True
        assert result["output"] == "raw output text"

    async def test_json_output_false_failure(self, server):
        proc = _make_process(returncode=1, stderr=b"permission denied")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["say", "hi"], json_output=False)
        assert success is False
        assert "permission denied" in result["error"]

    async def test_json_parse_error(self, server):
        proc = _make_process(stdout=b"not valid json")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["info"])
        assert success is False
        assert "Failed to parse" in result["error"]

    async def test_error_in_stdout_on_failure(self, server):
        error_data = json.dumps({"error": "session locked"}).encode()
        proc = _make_process(returncode=1, stdout=error_data)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            success, result = await server.run_agentwire_cmd(["ensure"])
        assert success is False
        assert result["error"] == "session locked"

    async def test_command_construction(self, server):
        proc = _make_process(stdout=b'{"ok": true}')
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await server.run_agentwire_cmd(["new", "-s", "test"])
            cmd_args = mock_exec.call_args[0]
            assert cmd_args == ("agentwire", "new", "-s", "test", "--json")

    async def test_command_no_json_flag(self, server):
        proc = _make_process(stdout=b"ok")
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await server.run_agentwire_cmd(["say", "hi"], json_output=False)
            cmd_args = mock_exec.call_args[0]
            assert "--json" not in cmd_args


class TestServiceAutostartAndWatchdog:
    """Portal-side service lifecycle (#214) — thin shells over the CLI."""

    async def test_autostart_calls_services_up_all(self, server, monkeypatch):
        # Collapse the politeness delay so the test is instant.
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        server.run_agentwire_cmd = AsyncMock(return_value=(True, {"results": [
            {"name": "tracker", "ok": True, "result": "started"},
        ]}))
        await server.autostart_custom_services()
        server.run_agentwire_cmd.assert_awaited_once_with(["services", "up", "--all"])

    async def test_notify_service_event_broadcasts_and_speaks(self, server):
        server.broadcast_dashboard = AsyncMock()
        server.run_agentwire_cmd = AsyncMock(return_value=(True, {}))
        await server._notify_service_event("tracker", "Service tracker is down", speak=True)
        # Toast stored + broadcast
        assert any(n["session"] == "service:tracker"
                   for n in server.active_notifications.values())
        server.broadcast_dashboard.assert_awaited()
        msg_type = server.broadcast_dashboard.await_args[0][0]
        assert msg_type == "notification"
        # TTS via the say CLI
        server.run_agentwire_cmd.assert_awaited_once_with(
            ["say", "Service tracker is down"], json_output=False)

    async def test_notify_replaces_stale_toast_for_same_service(self, server):
        server.broadcast_dashboard = AsyncMock()
        server.run_agentwire_cmd = AsyncMock(return_value=(True, {}))
        await server._notify_service_event("tracker", "down", speak=False)
        await server._notify_service_event("tracker", "recovered", speak=False)
        toasts = [n for n in server.active_notifications.values()
                  if n["session"] == "service:tracker"]
        assert len(toasts) == 1
        assert toasts[0]["text"] == "recovered"
