"""Integration tests for portal API handlers via aiohttp TestClient."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agentwire.config import load_config
from agentwire.server import AgentWireServer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(tmp_path, auth_token=None, allowed_origins=None):
    config = load_config(tmp_path / "nonexistent.yaml")
    # Override artifacts dir to use temp path
    config.artifacts = type(config.artifacts)(dir=tmp_path / "artifacts", max_size_mb=10)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    config.server.auth_token = auth_token
    if allowed_origins:
        config.server.allowed_origins = allowed_origins
    return config


@pytest.fixture
async def portal_client(tmp_path):
    """Create an AgentWireServer and wrap in TestClient."""
    server = AgentWireServer(_make_config(tmp_path))
    async with TestClient(TestServer(server.app)) as client:
        yield client, server


@pytest.fixture
async def portal_client_with_token(tmp_path):
    """Portal with bearer-token auth enforced."""
    server = AgentWireServer(_make_config(tmp_path, auth_token="testtoken123"))
    async with TestClient(TestServer(server.app)) as client:
        yield client, server


@pytest.fixture
async def portal_client_with_origins(tmp_path):
    """Portal with an allowed_origins entry (tunnel-domain case)."""
    server = AgentWireServer(
        _make_config(tmp_path, allowed_origins=["https://portal.example.com"])
    )
    async with TestClient(TestServer(server.app)) as client:
        yield client, server


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    async def test_health_returns_200(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/health")
        assert resp.status == 200

    async def test_health_json_format(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/health")
        data = await resp.json()
        assert data["status"] == "ok"
        assert "version" in data


# ---------------------------------------------------------------------------
# Sessions API
# ---------------------------------------------------------------------------


class TestApiSessions:
    async def test_sessions_list(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": [
                {"name": "app", "machine": None, "windows": 1, "path": "/app", "type": "claude-bypass"},
            ]})
            resp = await client.get("/api/sessions")
        assert resp.status == 200
        data = await resp.json()
        assert "machines" in data

    async def test_sessions_empty(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": []})
            resp = await client.get("/api/sessions")
        data = await resp.json()
        machines = data.get("machines", [])
        assert isinstance(machines, list)

    async def test_sessions_cli_failure(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (False, {"error": "tmux not running"})
            resp = await client.get("/api/sessions")
        data = await resp.json()
        assert data.get("machines") == []

    async def test_local_sessions_endpoint(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": [
                {"name": "test", "machine": None},
            ]})
            resp = await client.get("/api/sessions/local")
        assert resp.status == 200
        data = await resp.json()
        assert "sessions" in data


# ---------------------------------------------------------------------------
# Create session API
# ---------------------------------------------------------------------------


class TestApiCreateSession:
    async def test_create_minimal(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"session": "test", "path": "/p"})
            server.broadcast_dashboard = AsyncMock()
            resp = await client.post("/api/create", json={"name": "test"})
        assert resp.status == 200
        data = await resp.json()
        assert data.get("success") is True

    async def test_create_missing_name(self, portal_client):
        client, server = portal_client
        resp = await client.post("/api/create", json={"name": ""})
        data = await resp.json()
        assert "error" in data

    async def test_create_with_type(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"session": "test"})
            server.broadcast_dashboard = AsyncMock()
            resp = await client.post("/api/create", json={
                "name": "test", "type": "bare",
            })
        # Find the "new" call (not the "list" calls for sessions refresh)
        new_calls = [c for c in mock_cmd.call_args_list if c[0][0][0] == "new"]
        assert len(new_calls) >= 1
        args = new_calls[0][0][0]
        assert "--type" in args
        assert "bare" in args

    async def test_create_remote_session(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"session": "app@gpu"})
            server.broadcast_dashboard = AsyncMock()
            resp = await client.post("/api/create", json={
                "name": "app", "machine": "gpu",
            })
        data = await resp.json()
        assert data.get("success") is True

    async def test_create_worktree(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"session": "app/feature"})
            server.broadcast_dashboard = AsyncMock()
            resp = await client.post("/api/create", json={
                "name": "app", "worktree": True, "branch": "feature",
            })
        data = await resp.json()
        assert data.get("success") is True


# ---------------------------------------------------------------------------
# Close session API
# ---------------------------------------------------------------------------


class TestApiCloseSession:
    async def test_close_success(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {})
            server.broadcast_dashboard = AsyncMock()
            resp = await client.delete("/api/sessions/test-session")
        assert resp.status == 200
        data = await resp.json()
        assert data.get("success") is True

    async def test_close_cli_failure(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (False, {"error": "session not found"})
            resp = await client.delete("/api/sessions/bad-session")
        data = await resp.json()
        assert "error" in data

    async def test_close_cleans_up(self, portal_client):
        client, server = portal_client
        from agentwire.server import Session, SessionConfig
        server.active_sessions["test"] = Session(
            name="test", config=SessionConfig(), output_task=None,
        )
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {})
            server.broadcast_dashboard = AsyncMock()
            await client.delete("/api/sessions/test")
        assert "test" not in server.active_sessions


# ---------------------------------------------------------------------------
# Voices API
# ---------------------------------------------------------------------------


class TestApiVoices:
    async def test_voices_list(self, portal_client):
        client, server = portal_client
        with patch.object(server, "_get_voices", new_callable=AsyncMock) as mock_voices:
            mock_voices.return_value = ["alice", "bob"]
            resp = await client.get("/api/voices")
        assert resp.status == 200
        data = await resp.json()
        assert "alice" in data

    async def test_voices_empty(self, portal_client):
        client, server = portal_client
        with patch.object(server, "_get_voices", new_callable=AsyncMock) as mock_voices:
            mock_voices.return_value = []
            resp = await client.get("/api/voices")
        data = await resp.json()
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# Artifacts API
# ---------------------------------------------------------------------------


class TestApiArtifacts:
    async def test_upload_artifact(self, portal_client):
        client, server = portal_client
        resp = await client.post("/api/artifacts/upload", json={
            "filename": "test.html",
            "content": "<h1>Hello</h1>",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data.get("success") is True
        assert "/artifacts/test.html" in data.get("url", "")

    async def test_list_artifacts(self, portal_client):
        client, server = portal_client
        # Create a file first
        artifacts_dir = server.config.artifacts.dir
        (artifacts_dir / "demo.html").write_text("<p>hi</p>")
        resp = await client.get("/api/artifacts")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) >= 1
        assert data[0]["name"] == "demo.html"

    async def test_delete_artifact(self, portal_client):
        client, server = portal_client
        artifacts_dir = server.config.artifacts.dir
        (artifacts_dir / "deleteme.html").write_text("x")
        resp = await client.delete("/api/artifacts/deleteme.html")
        assert resp.status == 200
        data = await resp.json()
        assert data.get("success") is True
        assert not (artifacts_dir / "deleteme.html").exists()


# ---------------------------------------------------------------------------
# Desktop windows API
# ---------------------------------------------------------------------------


class TestApiDesktopWindows:
    async def test_windows_empty_no_clients(self, portal_client):
        client, server = portal_client
        resp = await client.get("/api/desktop/windows")
        assert resp.status == 200
        data = await resp.json()
        assert data.get("success") is True
        assert data.get("windows") == []


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------


class TestApiConfig:
    async def test_get_config(self, portal_client):
        client, server = portal_client
        resp = await client.get("/api/config")
        assert resp.status == 200
        data = await resp.json()
        assert "content" in data or "items" in data

    async def test_get_config_display_format(self, portal_client):
        client, server = portal_client
        resp = await client.get("/api/config?format=display")
        assert resp.status == 200
        data = await resp.json()
        assert "items" in data
        keys = [item["key"] for item in data["items"]]
        assert "TTS Backend" in keys


# ---------------------------------------------------------------------------
# Notify API
# ---------------------------------------------------------------------------


class TestApiNotify:
    async def test_accept_event(self, portal_client):
        client, server = portal_client
        server.broadcast_dashboard = AsyncMock()
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": []})
            resp = await client.post("/api/notify", json={
                "event": "session_created", "session": "test",
            })
        assert resp.status == 200
        data = await resp.json()
        assert data.get("success") is True

    async def test_missing_event(self, portal_client):
        client, server = portal_client
        resp = await client.post("/api/notify", json={"session": "test"})
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Security middleware: Origin validation (CSRF guard)
# ---------------------------------------------------------------------------


class TestOriginValidation:
    async def test_post_without_origin_allowed(self, portal_client):
        """curl/CLI requests don't send Origin and must keep working."""
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": []})
            server.broadcast_dashboard = AsyncMock()
            resp = await client.post("/api/notify", json={"event": "x"})
        assert resp.status != 403

    async def test_post_own_origin_allowed(self, portal_client):
        client, server = portal_client
        own = f"http://{client.host}:{client.port}"
        server.broadcast_dashboard = AsyncMock()
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": []})
            resp = await client.post(
                "/api/notify", json={"event": "x"}, headers={"Origin": own}
            )
        assert resp.status != 403

    async def test_post_evil_origin_rejected(self, portal_client):
        client, _ = portal_client
        resp = await client.post(
            "/api/notify", json={"event": "x"},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status == 403

    async def test_non_api_mutators_covered(self, portal_client):
        """Mutating routes outside /api/ (upload, transcribe) are guarded too."""
        client, _ = portal_client
        for path in ("/upload", "/transcribe"):
            resp = await client.post(
                path, headers={"Origin": "https://evil.example"}
            )
            assert resp.status == 403, path

    async def test_get_with_evil_origin_allowed(self, portal_client):
        """Origin check only applies to state-changing methods."""
        client, _ = portal_client
        resp = await client.get(
            "/health", headers={"Origin": "https://evil.example"}
        )
        assert resp.status == 200

    async def test_allowed_origins_entry(self, portal_client_with_origins):
        client, server = portal_client_with_origins
        server.broadcast_dashboard = AsyncMock()
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": []})
            resp = await client.post(
                "/api/notify", json={"event": "x"},
                headers={"Origin": "https://portal.example.com"},
            )
        assert resp.status != 403


# ---------------------------------------------------------------------------
# Security middleware: bearer-token auth
# ---------------------------------------------------------------------------


AUTH = {"Authorization": "Bearer testtoken123"}


class TestTokenAuth:
    async def test_missing_token_401(self, portal_client_with_token):
        client, _ = portal_client_with_token
        resp = await client.get("/api/sessions/local")
        assert resp.status == 401
        assert resp.headers["WWW-Authenticate"].startswith("Bearer")

    async def test_wrong_token_401(self, portal_client_with_token):
        client, _ = portal_client_with_token
        resp = await client.get(
            "/api/sessions/local", headers={"Authorization": "Bearer nope"}
        )
        assert resp.status == 401

    async def test_right_token_ok(self, portal_client_with_token):
        client, server = portal_client_with_token
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": []})
            resp = await client.get("/api/sessions/local", headers=AUTH)
        assert resp.status == 200

    async def test_mutation_requires_token(self, portal_client_with_token):
        client, _ = portal_client_with_token
        resp = await client.post("/api/notify", json={"event": "x"})
        assert resp.status == 401

    async def test_public_surface_open(self, portal_client_with_token):
        """The page shell + health must load so the token modal can render."""
        client, _ = portal_client_with_token
        assert (await client.get("/health")).status == 200
        assert (await client.get("/")).status == 200
        resp = await client.get("/static/js/api.js")
        assert resp.status != 401

    async def test_no_token_configured_open(self, portal_client):
        """Loopback default: no token configured behaves as before."""
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": []})
            resp = await client.get("/api/sessions/local")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Restricted mode (permission API)
# ---------------------------------------------------------------------------


class TestApiRestrictedMode:
    """Test the restricted mode command filtering through the permission endpoint."""

    async def test_artifact_upload_bad_filename(self, portal_client):
        client, server = portal_client
        resp = await client.post("/api/artifacts/upload", json={
            "filename": "../../../etc/passwd",
            "content": "evil",
        })
        assert resp.status == 400

    async def test_artifact_upload_missing_fields(self, portal_client):
        client, server = portal_client
        resp = await client.post("/api/artifacts/upload", json={})
        assert resp.status == 400

    async def test_artifact_delete_path_traversal(self, portal_client):
        client, server = portal_client
        resp = await client.delete("/api/artifacts/.hidden-file")
        # Regex rejects filenames starting with dot
        assert resp.status == 400


# ---------------------------------------------------------------------------
# /transcribe under the default tier
# ---------------------------------------------------------------------------


class TestTranscribeDefaultTier:
    async def test_transcribe_returns_501_without_custom_stt(self, portal_client):
        client, server = portal_client
        # Empty config → stt.backend "default" → NoSTT
        from agentwire.stt import NoSTT
        server.stt = NoSTT()

        resp = await client.post("/transcribe", data=b"fakeaudio")
        assert resp.status == 501
        body = await resp.json()
        assert "browser speech recognition" in body["error"]


# ---------------------------------------------------------------------------
# /api/voice-status
# ---------------------------------------------------------------------------


class TestVoiceStatus:
    async def test_default_tier_shape(self, portal_client):
        client, server = portal_client
        resp = await client.get("/api/voice-status")
        assert resp.status == 200
        body = await resp.json()
        assert body["stt"] == {"backend": "default", "url": None, "available": True}
        assert body["tts"]["backend"] == "default"
        assert body["tts"]["available"] is True
        assert body["instant_mode"] is True
        assert body["corrections"] == {}

    async def test_custom_tier_probes_shim(self, portal_client):
        client, server = portal_client
        server.config.stt.backend = "custom"
        server.config.stt.url = "http://localhost:8101"
        server.config.stt.corrections = {"team up": "tmux"}
        server.config.tts.backend = "custom"
        server.config.tts.url = "http://localhost:8100"

        async def fake_probe(base_url, path, timeout=1.5):
            if path == "/health":
                return {"status": "ok"}
            if path == "/capabilities":
                return {"tool_prompt": "supports [laugh]", "voices": ["amy"]}
            return None

        server._probe_shim = fake_probe

        resp = await client.get("/api/voice-status")
        body = await resp.json()
        assert body["instant_mode"] is False
        assert body["stt"]["available"] is True
        assert body["tts"]["tool_prompt"] == "supports [laugh]"
        assert body["tts"]["voices"] == ["amy"]
        assert body["corrections"] == {"team up": "tmux"}

    async def test_unreachable_shim_reports_unavailable(self, portal_client):
        client, server = portal_client
        server.config.tts.backend = "custom"
        server.config.tts.url = "http://localhost:9999"

        async def fake_probe(base_url, path, timeout=1.5):
            return None

        server._probe_shim = fake_probe

        resp = await client.get("/api/voice-status")
        body = await resp.json()
        assert body["tts"]["available"] is False
        assert "tool_prompt" not in body["tts"]

    async def test_result_is_cached(self, portal_client):
        client, server = portal_client
        calls = []

        async def fake_probe(base_url, path, timeout=1.5):
            calls.append(path)
            return {"status": "ok"}

        server.config.tts.backend = "custom"
        server.config.tts.url = "http://localhost:8100"
        server._probe_shim = fake_probe

        await client.get("/api/voice-status")
        first = len(calls)
        await client.get("/api/voice-status")
        assert len(calls) == first  # second hit served from cache

    async def test_api_voices_empty_in_default_tier(self, portal_client):
        client, server = portal_client
        resp = await client.get("/api/voices")
        assert resp.status == 200
        assert await resp.json() == []
