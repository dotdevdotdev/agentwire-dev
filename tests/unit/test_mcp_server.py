"""Tests for agentwire/mcp_server.py — format functions, run_agentwire_cmd, helpers."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Format functions — empty/missing-key/multi-entry behavior is parametrized;
# format-specific assertions kept as individual tests below.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn_name,list_key,empty_substring",
    [
        ("format_sessions", "sessions", "No active sessions"),
        ("format_panes", "panes", "No panes"),
        ("format_machines", "machines", "No remote machines"),
        ("format_projects", "projects", "No projects found"),
        ("format_roles", "roles", "No roles available"),
        ("format_voices", "voices", "No custom voices"),
    ],
)
class TestFormatEmpty:
    """All formatters return their empty message for [] and missing-key input."""

    def test_empty_list(self, fn_name, list_key, empty_substring):
        from agentwire import mcp_server
        fn = getattr(mcp_server, fn_name)
        # format_panes also reads "session" key — provide it harmlessly
        result = fn({list_key: [], "session": "test"})
        assert empty_substring in result

    def test_missing_key(self, fn_name, list_key, empty_substring):
        from agentwire import mcp_server
        fn = getattr(mcp_server, fn_name)
        result = fn({"session": "test"})
        assert empty_substring in result


@pytest.mark.parametrize(
    "fn_name,list_key,extra,entries",
    [
        ("format_sessions", "sessions", {}, [
            {"name": "a", "machine": None, "windows": 1, "path": "/a", "type": "bare"},
            {"name": "b", "machine": "m1", "windows": 2, "path": "/b", "type": "claude-bypass"},
        ]),
        ("format_panes", "panes", {"session": "s"}, [
            {"index": 0, "command": "claude", "active": False},
            {"index": 1, "command": "bash", "active": True},
        ]),
        ("format_machines", "machines", {}, [
            {"id": "a", "host": "1.1.1.1", "user": "u", "status": "ok"},
            {"id": "b", "host": "2.2.2.2", "user": "v", "status": "ok"},
        ]),
        ("format_projects", "projects", {}, [{"name": "a", "path": "/a"}, {"name": "b", "path": "/b"}]),
        ("format_roles", "roles", {}, [
            {"name": "a", "description": "da", "source": "s"},
            {"name": "b", "description": "db", "source": "s"},
        ]),
    ],
)
def test_format_multiple_produces_header_plus_one_line_per_entry(fn_name, list_key, extra, entries):
    """All listing formatters emit a header line then one line per entry."""
    from agentwire import mcp_server
    fn = getattr(mcp_server, fn_name)
    result = fn({list_key: entries, **extra})
    lines = result.split("\n")
    assert len(lines) == len(entries) + 1


@pytest.mark.parametrize(
    "fn_name,list_key,extra,entry",
    [
        ("format_sessions", "sessions", {}, {"name": "x"}),
        ("format_panes", "panes", {"session": "s"}, {}),
        ("format_machines", "machines", {}, {}),
        ("format_projects", "projects", {}, {}),
        ("format_roles", "roles", {}, {}),
        ("format_voices", "voices", {}, [{}]),
    ],
)
def test_format_missing_optional_fields_shows_unknown(fn_name, list_key, extra, entry):
    """Missing optional fields produce 'unknown' rather than crash or empty string."""
    from agentwire import mcp_server
    fn = getattr(mcp_server, fn_name)
    entries = entry if isinstance(entry, list) else [entry]
    result = fn({list_key: entries, **extra})
    assert "unknown" in result or "(local)" in result  # sessions uses (local) for null machine


# Format-specific assertions — keep separate; logic is unique per formatter.

class TestFormatSessionsBehavior:
    def test_all_fields_render(self):
        from agentwire.mcp_server import format_sessions
        result = format_sessions({"sessions": [
            {"name": "my-app", "machine": "gpu-box", "windows": 3, "path": "/p", "type": "claude-bypass"},
        ]})
        assert "my-app" in result
        assert "gpu-box" in result
        assert "3 window(s)" in result
        assert "type=claude-bypass" in result

    def test_null_machine_shows_local(self):
        from agentwire.mcp_server import format_sessions
        assert "local" in format_sessions({"sessions": [{"name": "x", "machine": None}]})


class TestFormatPanesBehavior:
    def test_pane_0_is_orchestrator_active_marked(self):
        from agentwire.mcp_server import format_panes
        result = format_panes({"panes": [{"index": 0, "command": "claude", "active": True}], "session": "s"})
        assert "[orchestrator]" in result
        assert "(active)" in result

    def test_pane_nonzero_is_worker(self):
        from agentwire.mcp_server import format_panes
        result = format_panes({"panes": [{"index": 1, "command": "bash"}], "session": "s"})
        assert "[worker]" in result


class TestFormatMachinesBehavior:
    def test_user_at_host_format(self):
        from agentwire.mcp_server import format_machines
        result = format_machines({"machines": [
            {"id": "gpu", "host": "10.0.0.1", "user": "root", "status": "online"}
        ]})
        assert "root@10.0.0.1" in result
        assert "status: online" in result

    def test_blank_user_omits_at_sign(self):
        from agentwire.mcp_server import format_machines
        result = format_machines({"machines": [
            {"id": "m1", "host": "h", "user": "", "status": "unknown"}
        ]})
        assert "m1: h" in result
        assert "@" not in result.split("m1: ")[1].split(" ")[0]


class TestFormatProjectsBehavior:
    def test_has_config_marker(self):
        from agentwire.mcp_server import format_projects
        result = format_projects({"projects": [{"name": "app", "path": "/app", "has_config": True}]})
        assert "(has .agentwire.yml)" in result

    def test_no_config_no_marker(self):
        from agentwire.mcp_server import format_projects
        result = format_projects({"projects": [{"name": "app", "path": "/app", "has_config": False}]})
        assert ".agentwire.yml" not in result


class TestFormatRolesBehavior:
    def test_full_role_format(self):
        from agentwire.mcp_server import format_roles
        result = format_roles({"roles": [{"name": "voice", "description": "Voice comms", "source": "bundled"}]})
        assert "voice: Voice comms (bundled)" in result


class TestFormatVoicesBehavior:
    @pytest.mark.parametrize("voices", [
        [{"name": "alice"}, {"name": "bob"}],
        ["alice", "bob"],
        [{"name": "alice"}, "bob"],
    ])
    def test_dict_string_and_mixed_entries_all_render(self, voices):
        from agentwire.mcp_server import format_voices
        result = format_voices({"voices": voices})
        assert "alice" in result
        assert "bob" in result


# ---------------------------------------------------------------------------
# run_agentwire_cmd
# ---------------------------------------------------------------------------


class TestRunAgentwireCmd:
    def setup_method(self):
        from agentwire.mcp_server import run_agentwire_cmd
        self.fn = run_agentwire_cmd

    @patch("agentwire.mcp_server.subprocess.run")
    def test_successful_json(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"success": true, "data": 1}',
            stderr="",
        )
        result = self.fn(["list"])
        assert result == {"success": True, "data": 1}
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["agentwire", "list", "--json"]

    @patch("agentwire.mcp_server.subprocess.run")
    def test_json_array_wrapping(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"id": 1}, {"id": 2}]',
            stderr="",
        )
        result = self.fn(["history", "list"])
        assert result["success"] is True
        assert result["items"] == [{"id": 1}, {"id": 2}]

    @patch("agentwire.mcp_server.subprocess.run")
    def test_json_without_success_key(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"data": "hello"}',
            stderr="",
        )
        result = self.fn(["info"])
        assert result["success"] is True
        assert result["data"] == "hello"

    @patch("agentwire.mcp_server.subprocess.run")
    def test_json_parse_failure_falls_back(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json",
            stderr="",
        )
        result = self.fn(["list"])
        assert result["success"] is True
        assert result["output"] == "not json"

    @patch("agentwire.mcp_server.subprocess.run")
    def test_nonzero_returncode(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="session not found",
        )
        result = self.fn(["kill", "-s", "x"])
        assert result["success"] is False
        assert "session not found" in result["error"]

    @patch("agentwire.mcp_server.subprocess.run")
    def test_json_output_false(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="raw output here",
            stderr="",
        )
        result = self.fn(["say", "hello"], json_output=False)
        assert result["success"] is True
        assert result["output"] == "raw output here"
        cmd = mock_run.call_args[0][0]
        assert "--json" not in cmd

    @patch("agentwire.mcp_server.subprocess.run")
    def test_timeout_expired(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="agentwire", timeout=30)
        result = self.fn(["long-cmd"])
        assert result["success"] is False
        assert "timed out" in result["error"]

    @patch("agentwire.mcp_server.subprocess.run")
    def test_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        result = self.fn(["list"])
        assert result["success"] is False
        assert "not found" in result["error"]

    @patch("agentwire.mcp_server.subprocess.run")
    def test_command_construction(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        self.fn(["new", "-s", "test"])
        cmd = mock_run.call_args[0][0]
        assert cmd == ["agentwire", "new", "-s", "test", "--json"]

    @patch("agentwire.mcp_server.subprocess.run")
    def test_custom_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        self.fn(["spawn"], timeout=120)
        assert mock_run.call_args[1]["timeout"] == 120

    @patch("agentwire.mcp_server.subprocess.run")
    def test_generic_exception(self, mock_run):
        mock_run.side_effect = OSError("permission denied")
        result = self.fn(["list"])
        assert result["success"] is False
        assert "permission denied" in result["error"]

    @patch("agentwire.mcp_server.subprocess.run")
    def test_empty_stdout_nonzero(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        result = self.fn(["x"])
        assert result["success"] is False


# ---------------------------------------------------------------------------
# get_portal_url
# ---------------------------------------------------------------------------


class TestGetPortalUrl:
    def setup_method(self):
        from agentwire.mcp_server import get_portal_url
        self.fn = get_portal_url

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_PORTAL_URL", "https://custom:9999")
        assert self.fn() == "https://custom:9999"

    def test_config_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_PORTAL_URL", raising=False)
        config_dir = tmp_path / ".agentwire"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        import yaml
        config_file.write_text(yaml.dump({"portal": {"url": "https://from-config:1234"}}))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert self.fn() == "https://from-config:1234"

    def test_default_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_PORTAL_URL", raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert self.fn() == "https://localhost:8765"

    def test_env_var_priority_over_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_PORTAL_URL", "https://env:1111")
        config_dir = tmp_path / ".agentwire"
        config_dir.mkdir()
        import yaml
        (config_dir / "config.yaml").write_text(yaml.dump({"portal": {"url": "https://cfg:2222"}}))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert self.fn() == "https://env:1111"

    def test_malformed_yaml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_PORTAL_URL", raising=False)
        config_dir = tmp_path / ".agentwire"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(": : : bad yaml {{{{")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert self.fn() == "https://localhost:8765"


# ---------------------------------------------------------------------------
# get_caller_session
# ---------------------------------------------------------------------------


class TestGetCallerSession:
    def setup_method(self):
        from agentwire.mcp_server import get_caller_session
        self.fn = get_caller_session

    def test_no_tmux_pane(self, monkeypatch):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        assert self.fn() is None

    @patch("agentwire.mcp_server.subprocess.run")
    def test_returns_session_name(self, mock_run, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%5")
        mock_run.return_value = MagicMock(returncode=0, stdout="my-session\n")
        assert self.fn() == "my-session"

    @patch("agentwire.mcp_server.subprocess.run")
    def test_empty_stdout(self, mock_run, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%5")
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert self.fn() is None

    @patch("agentwire.mcp_server.subprocess.run")
    def test_nonzero_returncode(self, mock_run, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%5")
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert self.fn() is None

    @patch("agentwire.mcp_server.subprocess.run")
    def test_timeout_expired(self, mock_run, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%5")
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tmux", timeout=2)
        assert self.fn() is None


# ---------------------------------------------------------------------------
# _portal_request
# ---------------------------------------------------------------------------


class TestPortalRequest:
    def setup_method(self):
        from agentwire.mcp_server import _portal_request
        self.fn = _portal_request

    @patch("agentwire.mcp_server.get_portal_url", return_value="https://localhost:8765")
    @patch("requests.get")
    def test_get_request(self, mock_get, mock_url):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "windows": []}
        mock_get.return_value = mock_resp
        result = self.fn("GET", "/api/desktop/windows")
        assert result == {"success": True, "windows": []}
        mock_get.assert_called_once_with(
            "https://localhost:8765/api/desktop/windows", verify=False, timeout=10
        )

    @patch("agentwire.mcp_server.get_portal_url", return_value="https://localhost:8765")
    @patch("requests.post")
    def test_post_request(self, mock_post, mock_url):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True}
        mock_post.return_value = mock_resp
        result = self.fn("POST", "/api/desktop/window/open", {"type": "session"})
        assert result["success"] is True
        mock_post.assert_called_once()

    @patch("agentwire.mcp_server.get_portal_url", return_value="https://localhost:8765")
    @patch("requests.get")
    def test_non_200_status(self, mock_get, mock_url):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp
        result = self.fn("GET", "/api/health")
        assert result["success"] is False
        assert "500" in result["error"]

    @patch("agentwire.mcp_server.get_portal_url", return_value="https://localhost:8765")
    @patch("requests.get")
    def test_connection_error(self, mock_get, mock_url):
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError()
        result = self.fn("GET", "/api/health")
        assert result["success"] is False
        assert "not reachable" in result["error"]

    @patch("agentwire.mcp_server.get_portal_url", return_value="https://localhost:8765")
    @patch("requests.get")
    def test_generic_exception(self, mock_get, mock_url):
        mock_get.side_effect = Exception("timeout")
        result = self.fn("GET", "/api/health")
        assert result["success"] is False
        assert "timeout" in result["error"]

    @patch("agentwire.mcp_server.get_portal_url", return_value="https://localhost:8765")
    @patch("requests.post")
    def test_post_default_empty_body(self, mock_post, mock_url):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True}
        mock_post.return_value = mock_resp
        self.fn("POST", "/api/path")
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {}
