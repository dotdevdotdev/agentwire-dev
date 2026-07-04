"""Integration tests for portal API handlers via aiohttp TestClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentwire.config import load_config
from agentwire.server import AgentWireServer, Session, SessionConfig

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


class TestPwaSurface:
    """PWA manifest + service worker + push API (#483)."""

    async def test_manifest_served_publicly(self, portal_client_with_token):
        client, _ = portal_client_with_token
        resp = await client.get("/manifest.webmanifest")  # no bearer
        assert resp.status == 200
        data = await resp.json()
        assert data["display"] == "standalone"
        assert data["start_url"] == "/"

    async def test_service_worker_served_with_root_scope(self, portal_client_with_token):
        client, _ = portal_client_with_token
        resp = await client.get("/service-worker.js")  # no bearer
        assert resp.status == 200
        assert resp.headers.get("Service-Worker-Allowed") == "/"

    async def test_push_config_reports_disabled_by_default(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/api/push/config")
        assert resp.status == 200
        data = await resp.json()
        assert data["enabled"] is False

    async def test_push_subscribe_validates_payload(self, portal_client):
        client, _ = portal_client
        resp = await client.post("/api/push/subscribe", json={"endpoint": ""})
        assert resp.status == 400

    async def test_push_subscribe_and_unsubscribe(self, portal_client, tmp_path, monkeypatch):
        from agentwire import push_store

        monkeypatch.setattr(push_store, "SUBSCRIPTIONS_FILE", tmp_path / "push.json")
        client, _ = portal_client
        sub = {"endpoint": "https://push.example/x", "keys": {"p256dh": "p", "auth": "a"}}
        resp = await client.post("/api/push/subscribe", json=sub)
        assert resp.status == 200
        assert (await resp.json())["success"] is True
        assert len(push_store.load(tmp_path / "push.json")) == 1

        resp = await client.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]})
        assert resp.status == 200
        assert (await resp.json())["removed"] is True


# ---------------------------------------------------------------------------
# Sessions API
# ---------------------------------------------------------------------------


class TestApiSessions:
    async def test_sessions_list(self, portal_client):
        client, server = portal_client
        with patch.object(server, "_list_local_sessions", new_callable=AsyncMock) as mock_local, \
             patch.object(server, "_list_remote_sessions", new_callable=AsyncMock) as mock_remote:
            mock_local.return_value = [
                {"name": "app", "machine": None, "windows": 1, "path": "/app", "type": "claude-bypass"},
            ]
            mock_remote.return_value = {}
            resp = await client.get("/api/sessions")
        assert resp.status == 200
        data = await resp.json()
        assert "machines" in data

    async def test_sessions_empty(self, portal_client):
        client, server = portal_client
        with patch.object(server, "_list_local_sessions", new_callable=AsyncMock) as mock_local, \
             patch.object(server, "_list_remote_sessions", new_callable=AsyncMock) as mock_remote:
            mock_local.return_value = []
            mock_remote.return_value = {}
            resp = await client.get("/api/sessions")
        data = await resp.json()
        machines = data.get("machines", [])
        assert isinstance(machines, list)

    async def test_sessions_listing_failure(self, portal_client):
        client, server = portal_client
        with patch.object(server, "_list_local_sessions", new_callable=AsyncMock) as mock_local:
            mock_local.side_effect = RuntimeError("tmux not running")
            resp = await client.get("/api/sessions")
        data = await resp.json()
        assert data.get("machines") == []

    async def test_local_sessions_endpoint(self, portal_client):
        client, server = portal_client
        with patch.object(server, "_list_local_sessions", new_callable=AsyncMock) as mock_local:
            mock_local.return_value = [{"name": "test", "machine": None}]
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
            await client.post("/api/create", json={
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

    async def test_create_with_first_message_schedules_send(self, portal_client):
        import asyncio

        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"session": "ideaproj", "path": "/p"})
            server.broadcast_dashboard = AsyncMock()
            resp = await client.post("/api/create", json={
                "name": "ideaproj", "first_message": "build a voice diary app",
            })
            data = await resp.json()
            assert data.get("success") is True
            assert data.get("first_message") == "pending"
            # Drain the background delivery task while the mock is active
            await asyncio.gather(*server._background_tasks)
        send_calls = [c for c in mock_cmd.call_args_list if c[0][0][0] == "send"]
        assert len(send_calls) == 1
        args = send_calls[0][0][0]
        assert args[:4] == ["send", "-s", "ideaproj", "--wait-ready"]
        assert "--" in args
        assert args[-1] == "build a voice diary app"

    async def test_create_first_message_remote_rejected(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            resp = await client.post("/api/create", json={
                "name": "app", "machine": "gpu", "first_message": "an idea",
            })
        data = await resp.json()
        assert "error" in data
        # Rejected before any CLI call
        new_calls = [c for c in mock_cmd.call_args_list if c[0][0] and c[0][0][0] == "new"]
        assert new_calls == []

    async def test_create_first_message_failure_posts_toast(self, portal_client):
        import asyncio

        client, server = portal_client

        async def cmd_router(args, json_output=True):
            if args[0] == "send":
                return (False, {"error": "Agent in 'ideaproj' not ready after 60s"})
            return (True, {"session": "ideaproj", "path": "/p"})

        with patch.object(server, "run_agentwire_cmd", side_effect=cmd_router):
            server.broadcast_dashboard = AsyncMock()
            resp = await client.post("/api/create", json={
                "name": "ideaproj", "first_message": "an idea",
            })
            assert (await resp.json()).get("success") is True
            await asyncio.gather(*server._background_tasks)
            toasts = [n for n in server.active_notifications.values()
                      if n["session"] == "ideaproj"]
            assert len(toasts) == 1
            assert "not delivered" in toasts[0]["text"]
            notify_calls = [c for c in server.broadcast_dashboard.call_args_list
                            if c[0][0] == "notification"]
            assert len(notify_calls) == 1

    async def test_create_without_first_message_no_background_task(self, portal_client):
        client, server = portal_client
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"session": "plain"})
            server.broadcast_dashboard = AsyncMock()
            resp = await client.post("/api/create", json={"name": "plain"})
        data = await resp.json()
        assert data.get("first_message") is None
        assert server._background_tasks == set()


# ---------------------------------------------------------------------------
# Active-session shadow file API
# ---------------------------------------------------------------------------


class TestApiActiveSession:
    async def test_writes_shadow_file(self, portal_client, monkeypatch, tmp_path):
        client, server = portal_client
        monkeypatch.setenv("HOME", str(tmp_path))
        resp = await client.post("/api/active-session", json={"session": "agentwire-dev"})
        assert resp.status == 200
        data = await resp.json()
        assert data.get("success") is True
        assert data.get("session") == "agentwire-dev"
        shadow = tmp_path / ".agentwire" / "active-session"
        assert shadow.read_text().strip() == "agentwire-dev"

    async def test_missing_session(self, portal_client, monkeypatch, tmp_path):
        client, server = portal_client
        monkeypatch.setenv("HOME", str(tmp_path))
        resp = await client.post("/api/active-session", json={"session": ""})
        assert resp.status == 400
        data = await resp.json()
        assert data.get("success") is False

    async def test_overwrite_atomic(self, portal_client, monkeypatch, tmp_path):
        client, server = portal_client
        monkeypatch.setenv("HOME", str(tmp_path))
        await client.post("/api/active-session", json={"session": "first"})
        await client.post("/api/active-session", json={"session": "second"})
        shadow = tmp_path / ".agentwire" / "active-session"
        assert shadow.read_text().strip() == "second"
        # No leftover temp file from the atomic write.
        assert not (tmp_path / ".agentwire" / "active-session.tmp").exists()


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


class TestApiArtifactDownload:
    """GET /api/artifacts/download/{path} — attachment download (#707)."""

    async def test_download_artifact(self, portal_client):
        client, server = portal_client
        (server.config.artifacts.dir / "report.html").write_text("<h1>report</h1>")
        resp = await client.get("/api/artifacts/download/report.html")
        assert resp.status == 200
        disposition = resp.headers["Content-Disposition"]
        assert disposition.startswith("attachment")
        assert 'filename="report.html"' in disposition
        assert await resp.text() == "<h1>report</h1>"

    async def test_download_nested_entry_html(self, portal_client):
        """Multi-file artifact dirs: the entry HTML downloads by relative path."""
        client, server = portal_client
        bundle = server.config.artifacts.dir / "handoff-demo"
        bundle.mkdir()
        (bundle / "show-the-story.html").write_text("<p>story</p>")
        resp = await client.get("/api/artifacts/download/handoff-demo/show-the-story.html")
        assert resp.status == 200
        assert 'filename="show-the-story.html"' in resp.headers["Content-Disposition"]
        assert await resp.text() == "<p>story</p>"

    async def test_download_traversal_rejected(self, portal_client, tmp_path):
        client, server = portal_client
        secret = tmp_path / "secret.txt"
        secret.write_text("nope")
        # Encoded-slash traversal survives client/proxy dot-segment
        # normalization; aiohttp decodes match_info to "../secret.txt" — the
        # resolve/relative_to guard must reject it.
        resp = await client.get("/api/artifacts/download/..%2fsecret.txt")
        assert resp.status == 400
        body = await resp.json()
        assert body == {"success": False, "error": "invalid path"}

    async def test_download_symlink_escape_rejected(self, portal_client, tmp_path):
        client, server = portal_client
        secret = tmp_path / "secret.txt"
        secret.write_text("nope")
        (server.config.artifacts.dir / "link.html").symlink_to(secret)
        resp = await client.get("/api/artifacts/download/link.html")
        assert resp.status == 400

    async def test_download_missing_file_404(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/api/artifacts/download/nope.html")
        assert resp.status == 404

    async def test_download_requires_token(self, portal_client_with_token):
        client, server = portal_client_with_token
        (server.config.artifacts.dir / "auth.html").write_text("<p>x</p>")
        resp = await client.get("/api/artifacts/download/auth.html")
        assert resp.status == 401
        resp = await client.get(
            "/api/artifacts/download/auth.html",
            headers={"Authorization": "Bearer testtoken123"},
        )
        assert resp.status == 200
        assert resp.headers["Content-Disposition"].startswith("attachment")


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

    async def test_generic_event_reports_client_count(self, portal_client):
        """#444: a broadcast reports how many dashboards received it, so the
        caller knows whether anything actually saw the ephemeral event."""
        client, server = portal_client
        server.dashboard_clients = {object(), object()}
        server.broadcast_dashboard = AsyncMock()
        resp = await client.post("/api/notify", json={"event": "agent_progress"})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert data["clients"] == 2


class TestApiDesktopNotification:
    async def test_toast_reports_client_count(self, portal_client):
        """#444: posting a toast reports how many dashboards saw it live (the
        toast is persisted, so 0 isn't a failure — but the caller is told)."""
        client, server = portal_client
        server.dashboard_clients = {object()}
        server.broadcast_dashboard = AsyncMock()
        resp = await client.post("/api/desktop/notification", json={"text": "hi"})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert data["clients"] == 1
        assert "id" in data


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
        """The page shells + health must load so the token modal can render."""
        client, _ = portal_client_with_token
        assert (await client.get("/health")).status == 200
        assert (await client.get("/")).status == 200
        assert (await client.get("/mobile")).status == 200
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
# /mobile page (#279 — phone PTT surface)
# ---------------------------------------------------------------------------


class TestMobilePage:
    async def test_mobile_serves_shell(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/mobile")
        assert resp.status == 200
        body = await resp.text()
        assert "/static/js/mobile.js" in body
        assert "pttButton" in body
        # Sessions | Services tabs (#288) — Sessions selected by default
        assert 'id="tabSessions"' in body
        assert 'id="tabServices"' in body
        assert '<button class="mobile-tab selected" id="tabSessions"' in body

    async def test_mobile_no_cache(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/mobile")
        assert "no-store" in resp.headers.get("Cache-Control", "")

    async def test_mobile_public_with_token(self, portal_client_with_token):
        """Same exposure class as `/` — the shell loads without a token so
        the token modal can render; the APIs it calls stay guarded."""
        client, _ = portal_client_with_token
        resp = await client.get("/mobile")
        assert resp.status == 200

    async def test_mobile_only_get_is_public(self, portal_client_with_token):
        """_is_public_path is GET-only — a POST to /mobile still needs auth."""
        client, _ = portal_client_with_token
        resp = await client.post("/mobile")
        assert resp.status == 401


# ---------------------------------------------------------------------------
# Session state computation (#290 — mobile state visuals)
# ---------------------------------------------------------------------------


class TestSessionStateComputation:
    """Precedence: off → needs_input → working → idle; off on any doubt."""

    def _compute(self, tmp_path, monkeypatch, sessions, *, agent=True, markers=None):
        from agentwire import prompt_router

        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: agent)
        monkeypatch.setattr(prompt_router, "list_markers", lambda: markers or [])
        server = AgentWireServer(_make_config(tmp_path))
        server._compute_session_states(sessions)
        return server

    def test_off_when_no_agent_in_pane0(self, tmp_path, monkeypatch):
        sessions = [{"name": "foo"}]
        self._compute(tmp_path, monkeypatch, sessions, agent=False)
        assert sessions[0]["state"] == "off"

    def test_off_beats_marker(self, tmp_path, monkeypatch):
        """A stale marker on an agent-less session must not show needs_input."""
        sessions = [{"name": "foo"}]
        self._compute(
            tmp_path, monkeypatch, sessions, agent=False,
            markers=[{"session": "foo", "pane": 0, "kind": "permission"}],
        )
        assert sessions[0]["state"] == "off"

    def test_off_on_classification_error(self, tmp_path, monkeypatch):
        """When in doubt show off — never a false working/idle."""
        from agentwire import prompt_router

        def boom(s, p):
            raise RuntimeError("tmux exploded")

        monkeypatch.setattr(prompt_router, "is_agent_pane", boom)
        monkeypatch.setattr(prompt_router, "list_markers", lambda: [])
        server = AgentWireServer(_make_config(tmp_path))
        sessions = [{"name": "foo"}]
        server._compute_session_states(sessions)
        assert sessions[0]["state"] == "off"

    def test_needs_input_from_marker_with_hint(self, tmp_path, monkeypatch):
        sessions = [{"name": "foo"}]
        self._compute(
            tmp_path, monkeypatch, sessions,
            markers=[{
                "session": "foo", "pane": 0, "kind": "permission",
                "question": "Claude wants to run: rm build/",
            }],
        )
        assert sessions[0]["state"] == "needs_input"
        assert sessions[0]["state_kind"] == "permission"
        assert sessions[0]["state_hint"] == "Claude wants to run: rm build/"

    def test_needs_input_from_pending_permission(self, tmp_path, monkeypatch):
        from agentwire.server import PendingPermission, Session, SessionConfig

        sessions = [{"name": "foo"}]
        server = self._compute(tmp_path, monkeypatch, [])
        session = Session(name="foo", config=SessionConfig())
        session.pending_permission = PendingPermission(request={"tool_name": "Bash"})
        server.active_sessions["foo"] = session
        server._compute_session_states(sessions)
        assert sessions[0]["state"] == "needs_input"
        assert sessions[0]["state_kind"] == "permission"
        assert "Bash" in sessions[0]["state_hint"]

    def test_working_from_recent_output(self, tmp_path, monkeypatch):
        import time

        sessions = [{"name": "foo"}]
        server = self._compute(tmp_path, monkeypatch, [])
        server.session_activity["foo"] = {"last_output_timestamp": time.time()}
        server._compute_session_states(sessions)
        assert sessions[0]["state"] == "working"

    def test_idle_when_agent_quiet(self, tmp_path, monkeypatch):
        sessions = [{"name": "foo"}]
        self._compute(tmp_path, monkeypatch, sessions)
        assert sessions[0]["state"] == "idle"
        assert sessions[0]["state_kind"] is None
        assert sessions[0]["state_hint"] is None


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
# /api/voice-status
# ---------------------------------------------------------------------------


class TestVoiceStatus:
    async def test_default_tier_shim_loading(self, portal_client):
        client, server = portal_client

        # Default tier probes the managed shims' /health like custom does — STT
        # at :8101, TTS (Kokoro) at :8102. While they load (or haven't spawned),
        # /health isn't "ok" → server_transcribe False, so the client stays on
        # browser speech recognition and instant mode holds. No `moonshine` key.
        async def fake_probe(base_url, path, timeout=1.5):
            assert base_url in ("http://localhost:8101", "http://localhost:8102")
            return {"status": "loading", "percent": 40}

        server._probe_shim = fake_probe

        resp = await client.get("/api/voice-status")
        assert resp.status == 200
        body = await resp.json()
        assert body["stt"] == {
            "backend": "default",
            "url": None,
            "available": True,
            "server_transcribe": False,
        }
        assert body["tts"]["backend"] == "default"
        assert body["tts"]["available"] is True
        # Kokoro shim warm-up state surfaced from its /health, no `voices` yet.
        assert body["tts"]["kokoro"] == {"state": "loading", "percent": 40}
        assert "voices" not in body["tts"]
        assert body["instant_mode"] is True
        assert body["corrections"] == {}

    async def test_default_tier_shim_absent(self, portal_client):
        client, server = portal_client

        # Shim not spawned / unreachable → probe returns None → fall back.
        async def fake_probe(base_url, path, timeout=1.5):
            return None

        server._probe_shim = fake_probe

        resp = await client.get("/api/voice-status")
        body = await resp.json()
        assert body["stt"]["server_transcribe"] is False
        assert body["stt"]["available"] is True
        assert body["instant_mode"] is True
        # TTS shim absent too → kokoro state "absent", browser fallback stands.
        assert body["tts"]["kokoro"]["state"] == "absent"
        assert body["tts"]["available"] is True

    async def test_default_tier_shim_ok(self, portal_client):
        client, server = portal_client

        # Shim /health ok → host transcription takes over: server_transcribe
        # True and instant mode drops (audio now uploads).
        async def fake_probe(base_url, path, timeout=1.5):
            return {"status": "ok"}

        server._probe_shim = fake_probe

        resp = await client.get("/api/voice-status")
        body = await resp.json()
        assert body["stt"]["server_transcribe"] is True
        assert "moonshine" not in body["stt"]
        assert body["instant_mode"] is False
        # Kokoro shim ready → its preset voices are surfaced for the picker.
        assert body["tts"]["kokoro"]["state"] == "ok"
        assert body["tts"]["voices"]

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


# ---------------------------------------------------------------------------
# Default-tier managed STT shim lifecycle (ensure_managed_stt)
# ---------------------------------------------------------------------------


class TestEnsureManagedStt:
    async def test_delegates_to_stt_start_cli(self, portal_client, monkeypatch):
        client, server = portal_client
        # Skip the post-bind settle delay.
        monkeypatch.setattr("agentwire.server.asyncio.sleep", AsyncMock())

        calls = []

        async def fake_cmd(args, json_output=True):
            calls.append((args, json_output))
            return True, {"output": "STT server starting"}

        server.run_agentwire_cmd = fake_cmd
        server._voice_status_cache = (12345.0, {"stale": True})

        await server.ensure_managed_stt()

        # Idempotent CLI spawn — reuses cmd_stt_start (early-returns if the
        # agentwire-stt tmux session already exists).
        assert calls == [(["stt", "start"], False)]
        # Cache invalidated so the next voice-status poll re-probes /health.
        assert server._voice_status_cache is None

    async def test_cli_failure_is_soft(self, portal_client, monkeypatch):
        client, server = portal_client
        monkeypatch.setattr("agentwire.server.asyncio.sleep", AsyncMock())

        async def fake_cmd(args, json_output=True):
            return False, {"error": "boom"}

        server.run_agentwire_cmd = fake_cmd
        # Must not raise — browser STT keeps working.
        await server.ensure_managed_stt()
        assert server._voice_status_cache is None

    def test_spawn_gate_is_moonshine_importable(self):
        # run_server schedules ensure_managed_stt only when
        # `config.stt.backend == "default" and moonshine_importable()`. This is
        # the gate it evaluates — proven importable in this (py<3.14) env.
        from agentwire.stt import moonshine_importable

        assert moonshine_importable() is True


class TestStopManagedStt:
    """--no-stt must stop a shim left running by a previous portal (#679)."""

    async def test_delegates_to_stt_stop_cli(self, portal_client):
        client, server = portal_client
        calls = []

        async def fake_cmd(args, json_output=True):
            calls.append((args, json_output))
            return True, {"output": "STT server stopped."}

        server.run_agentwire_cmd = fake_cmd
        await server.stop_managed_stt()
        assert calls == [(["stt", "stop"], False)]

    async def test_not_running_is_soft(self, portal_client):
        client, server = portal_client

        async def fake_cmd(args, json_output=True):
            return False, {"error": "STT server is not running."}

        server.run_agentwire_cmd = fake_cmd
        # Must not raise — nothing to stop is the common case.
        await server.stop_managed_stt()


# ---------------------------------------------------------------------------
# CLI overrides (--no-stt / --no-tts) — apply_cli_overrides
# ---------------------------------------------------------------------------


class TestApplyCliOverrides:
    @staticmethod
    def _config():
        from types import SimpleNamespace

        return SimpleNamespace(
            server=SimpleNamespace(host="127.0.0.1", port=8000),
            tts=SimpleNamespace(backend="default", url=None),
            stt=SimpleNamespace(backend="default", url=None),
        )

    def test_no_stt_disables_backend_and_shim_gate(self):
        from agentwire.server import apply_cli_overrides

        config = self._config()
        apply_cli_overrides(config, {"no_stt": True})
        assert config.stt.backend == "none"
        assert config.stt.url is None
        # This is the run_server autostart gate for ensure_managed_stt —
        # with backend "none" the Moonshine shim must not be scheduled.
        assert not (config.stt.backend == "default")

    def test_no_tts_unchanged(self):
        from agentwire.server import apply_cli_overrides

        config = self._config()
        apply_cli_overrides(config, {"no_tts": True})
        assert config.tts.backend == "none"
        assert config.stt.backend == "default"

    def test_both_disabled(self):
        from agentwire.server import apply_cli_overrides

        config = self._config()
        apply_cli_overrides(config, {"no_tts": True, "no_stt": True})
        assert config.tts.backend == "none"
        assert config.stt.backend == "none"

    def test_no_overrides_is_noop(self):
        from agentwire.server import apply_cli_overrides

        config = self._config()
        apply_cli_overrides(config, {})
        assert config.stt.backend == "default"
        assert config.tts.backend == "default"
        assert config.server.port == 8000


# ---------------------------------------------------------------------------
# Default-tier managed Kokoro TTS shim lifecycle (ensure_managed_tts)
# ---------------------------------------------------------------------------


class TestEnsureManagedTts:
    async def test_delegates_to_kokoro_start_cli(self, portal_client, monkeypatch):
        client, server = portal_client
        # Skip the post-bind settle delay.
        monkeypatch.setattr("agentwire.server.asyncio.sleep", AsyncMock())

        calls = []

        async def fake_cmd(args, json_output=True):
            calls.append((args, json_output))
            return True, {"output": "Kokoro TTS shim starting"}

        server.run_agentwire_cmd = fake_cmd
        server._voice_status_cache = (12345.0, {"stale": True})

        await server.ensure_managed_tts()

        # Idempotent CLI spawn — reuses cmd_kokoro_start (early-returns if the
        # agentwire-kokoro tmux session already exists).
        assert calls == [(["kokoro", "start"], False)]
        # Cache invalidated so the next voice-status poll re-probes /health.
        assert server._voice_status_cache is None

    async def test_cli_failure_is_soft(self, portal_client, monkeypatch):
        client, server = portal_client
        monkeypatch.setattr("agentwire.server.asyncio.sleep", AsyncMock())

        async def fake_cmd(args, json_output=True):
            return False, {"error": "boom"}

        server.run_agentwire_cmd = fake_cmd
        # Must not raise — browser speechSynthesis keeps working.
        await server.ensure_managed_tts()
        assert server._voice_status_cache is None

    def test_spawn_gate_is_kokoro_importable(self):
        # run_server schedules ensure_managed_tts only when
        # `config.tts.backend == "default" and kokoro_importable()`. This is
        # the gate it evaluates — proven importable in this (py<3.14) env.
        from agentwire.tts import kokoro_importable

        assert kokoro_importable() is True

    def test_spawn_gate_false_when_not_importable(self, monkeypatch):
        import agentwire.stt as stt_pkg

        monkeypatch.setattr(stt_pkg, "moonshine_importable", lambda: False)
        assert stt_pkg.moonshine_importable() is False


# ---------------------------------------------------------------------------
# Custom-tier voices cache (#271 — unreachable shim must not stall page load)
# ---------------------------------------------------------------------------


class TestVoicesCache:
    async def test_custom_tier_voices_cached(self, portal_client):
        client, server = portal_client
        calls = []

        async def fake_fetch():
            calls.append(1)
            return ["alpha", "beta"]

        server.config.tts.backend = "custom"
        server.config.tts.url = "http://localhost:8100"
        server._tts_get_voices = fake_fetch

        resp = await client.get("/api/voices")
        assert await resp.json() == ["alpha", "beta"]
        resp = await client.get("/api/voices")
        assert await resp.json() == ["alpha", "beta"]
        assert len(calls) == 1  # second hit served from cache

    async def test_cache_expires(self, portal_client):
        client, server = portal_client
        calls = []

        async def fake_fetch():
            calls.append(1)
            return ["alpha"]

        server.config.tts.backend = "custom"
        server.config.tts.url = "http://localhost:8100"
        server._tts_get_voices = fake_fetch

        await client.get("/api/voices")
        ts, voices = server._voices_cache
        server._voices_cache = (ts - 31, voices)  # age past the 30s TTL
        await client.get("/api/voices")
        assert len(calls) == 2

    async def test_default_tier_never_fetches(self, portal_client):
        client, server = portal_client

        async def fail_fetch():
            raise AssertionError("default tier must not hit the shim")

        server._tts_get_voices = fail_fetch
        resp = await client.get("/api/voices")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Permission endpoints — parent routing + conditional respond (#276)
# ---------------------------------------------------------------------------


class TestPermissionRouting:
    async def _start_request(self, client, server, session="myproj", pane=2,
                             tmux_session="real-sess"):
        import asyncio

        task = asyncio.create_task(client.post(f"/api/permission/{session}", json={
            "tool_name": "Bash",
            "tool_input": {"command": "git push"},
            "pane_index": pane,
            "tmux_session": tmux_session,
        }))
        for _ in range(200):
            await asyncio.sleep(0.02)
            sess = server.active_sessions.get(session)
            if sess and sess.pending_permission:
                return task, sess
        raise AssertionError("pending_permission never appeared")

    async def test_request_routes_to_parent_and_respond_sends_pane_keystroke(
        self, portal_client
    ):
        client, server = portal_client
        server._say_to_room = AsyncMock()
        with patch(
            "agentwire.server.prompt_router.notify_permission_request",
            return_value="orch",
        ) as notify, patch(
            "agentwire.server.prompt_router.clear_marker"
        ) as clear, patch(
            "agentwire.server.prompt_router.screen_shows_live_menu",
            return_value=True,
        ), patch(
            "agentwire.server.prompt_router._capture", return_value=""
        ):
            run = server._run_subprocess = AsyncMock(return_value=(0, "", ""))
            task, sess = await self._start_request(client, server)
            assert sess.pending_permission.pane_index == 2

            # Routed with the real tmux session name + pane from the hook.
            notify.assert_called_once()
            assert notify.call_args[0][0] == "real-sess"
            assert notify.call_args[0][1] == 2

            resp = await client.post(
                "/api/permission/myproj/respond", json={"decision": "allow"}
            )
            assert resp.status == 200
            body = await (await task).json()
            assert body["decision"] == "allow"

            keystroke_calls = [
                c for c in run.call_args_list
                if c.args and c.args[0][:2] == ["agentwire", "send-keys"]
            ]
            assert keystroke_calls, "no keystroke sent"
            assert keystroke_calls[0].args[0] == [
                "agentwire", "send-keys", "-s", "real-sess", "--pane", "2", "1"
            ]
            clear.assert_called()

    async def test_respond_skips_keystroke_when_dialog_gone(self, portal_client):
        client, server = portal_client
        server._say_to_room = AsyncMock()
        with patch(
            "agentwire.server.prompt_router.notify_permission_request",
            return_value=None,
        ), patch(
            "agentwire.server.prompt_router.clear_marker"
        ), patch(
            # Parent (or human in the terminal) already answered: no live
            # menu on the pane — a late keystroke must NOT be sent.
            "agentwire.server.prompt_router.screen_shows_live_menu",
            return_value=False,
        ), patch(
            "agentwire.server.prompt_router._capture", return_value=""
        ):
            run = server._run_subprocess = AsyncMock(return_value=(0, "", ""))
            task, sess = await self._start_request(client, server)
            resp = await client.post(
                "/api/permission/myproj/respond", json={"decision": "deny"}
            )
            assert resp.status == 200
            body = await (await task).json()
            assert body["decision"] == "deny"

            keystroke_calls = [
                c for c in run.call_args_list
                if c.args and c.args[0][:2] == ["agentwire", "send-keys"]
            ]
            assert keystroke_calls == []

    async def test_broadcast_carries_parent_notified(self, portal_client):
        client, server = portal_client
        server._say_to_room = AsyncMock()
        broadcasts = []

        async def fake_broadcast(session, message):
            broadcasts.append(message)

        server._broadcast = fake_broadcast
        with patch(
            "agentwire.server.prompt_router.notify_permission_request",
            return_value="orch",
        ), patch(
            "agentwire.server.prompt_router.clear_marker"
        ), patch(
            "agentwire.server.prompt_router.screen_shows_live_menu",
            return_value=True,
        ), patch(
            "agentwire.server.prompt_router._capture", return_value=""
        ):
            server._run_subprocess = AsyncMock(return_value=(0, "", ""))
            task, sess = await self._start_request(client, server)
            requests = [m for m in broadcasts if m.get("type") == "permission_request"]
            assert requests and requests[0]["parent_notified"] == "orch"
            await client.post(
                "/api/permission/myproj/respond", json={"decision": "allow"}
            )
            await task


# ---------------------------------------------------------------------------
# #425 — frozen security-critical config
# ---------------------------------------------------------------------------


class TestFrozenConfigEndpoint:
    def _write_config(self, monkeypatch, tmp_path, body):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = tmp_path / ".agentwire" / "config.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(body)
        return cfg

    async def test_changing_auth_token_rejected(self, portal_client, monkeypatch, tmp_path):
        client, server = portal_client
        cfg = self._write_config(
            monkeypatch, tmp_path,
            'server:\n  host: "127.0.0.1"\n  auth_token: "real"\n',
        )
        resp = await client.post("/api/config", json={
            "content": 'server:\n  host: "127.0.0.1"\n  auth_token: ""\n',
        })
        assert resp.status == 403
        data = await resp.json()
        assert "server.auth_token" in data["frozen_keys"]
        # On-disk config untouched.
        assert 'auth_token: "real"' in cfg.read_text()

    async def test_changing_executables_rejected(self, portal_client, monkeypatch, tmp_path):
        client, server = portal_client
        self._write_config(monkeypatch, tmp_path, "executables:\n  claude: /usr/bin/claude\n")
        resp = await client.post("/api/config", json={
            "content": "executables:\n  claude: /tmp/evil\n",
        })
        assert resp.status == 403
        assert "executables" in (await resp.json())["frozen_keys"]

    async def test_non_frozen_change_allowed(self, portal_client, monkeypatch, tmp_path):
        client, server = portal_client
        cfg = self._write_config(monkeypatch, tmp_path, "server:\n  port: 8765\n")
        resp = await client.post("/api/config", json={
            "content": "server:\n  port: 9000\n",
        })
        assert resp.status == 200
        assert "9000" in cfg.read_text()

    async def test_safety_config_frozen(self, portal_client):
        client, server = portal_client
        resp = await client.post("/api/safety/config", json={"enabled": False})
        assert resp.status == 403
        assert "safety" in (await resp.json())["frozen_keys"]


# ---------------------------------------------------------------------------
# #423 — device pairing + per-device credentials
# ---------------------------------------------------------------------------


class TestPairingEndpoint:
    async def test_pair_with_valid_code_mints_token(self, portal_client_with_token):
        from agentwire.devices import create_pairing

        client, server = portal_client_with_token
        pairing = create_pairing("phone")
        # /api/pair is public — no bearer required.
        resp = await client.post("/api/pair", json={"code": pairing.code})
        assert resp.status == 200
        data = await resp.json()
        assert data["token"]
        assert data["device"]["name"] == "phone"
        assert "token_hash" not in data["device"]

    async def test_pair_with_bad_code_rejected(self, portal_client_with_token):
        client, _ = portal_client_with_token
        resp = await client.post("/api/pair", json={"code": "BOGUS123"})
        assert resp.status == 403

    async def test_pair_missing_code_400(self, portal_client_with_token):
        client, _ = portal_client_with_token
        resp = await client.post("/api/pair", json={})
        assert resp.status == 400

    async def test_pair_rate_limited(self, portal_client_with_token):
        """S1: the public token-minting endpoint throttles brute-force attempts."""
        client, server = portal_client_with_token
        cap = server._PAIR_PER_IP
        # Exhaust the per-IP budget with bad codes (each a recorded attempt).
        for _ in range(cap):
            r = await client.post("/api/pair", json={"code": "BADCODE0"})
            assert r.status == 403  # invalid code, but counted
        # The next attempt is throttled before the code is even checked.
        r = await client.post("/api/pair", json={"code": "BADCODE0"})
        assert r.status == 429


class TestPerDeviceAuth:
    async def test_paired_device_token_works(self, portal_client_with_token):
        from agentwire import devices
        from agentwire.devices import DeviceRegistry

        client, server = portal_client_with_token
        reg = DeviceRegistry.load()
        device, token = reg.add("laptop")
        devices._cache.clear()
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": []})
            resp = await client.get(
                "/api/sessions/local",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status == 200

    async def test_revoked_device_gets_401(self, portal_client_with_token):
        from agentwire import devices
        from agentwire.devices import DeviceRegistry

        client, server = portal_client_with_token
        reg = DeviceRegistry.load()
        device, token = reg.add("laptop")
        devices._cache.clear()
        reg.revoke(device.id)
        devices._cache.clear()
        resp = await client.get(
            "/api/sessions/local", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status == 401

    async def test_revoke_one_keeps_other(self, portal_client_with_token):
        from agentwire import devices
        from agentwire.devices import DeviceRegistry

        client, server = portal_client_with_token
        reg = DeviceRegistry.load()
        d1, t1 = reg.add("laptop")
        d2, t2 = reg.add("phone")
        reg.revoke(d1.id)
        devices._cache.clear()
        with patch.object(server, "run_agentwire_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (True, {"sessions": []})
            assert (await client.get(
                "/api/sessions/local", headers={"Authorization": f"Bearer {t1}"}
            )).status == 401
            assert (await client.get(
                "/api/sessions/local", headers={"Authorization": f"Bearer {t2}"}
            )).status == 200




# ---------------------------------------------------------------------------
# Static assets: WebP icons, gzip, Cache-Control (#488)
# ---------------------------------------------------------------------------


class TestStaticAssets:
    async def test_icons_listing_returns_webp(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/api/icons/sessions")
        assert resp.status == 200
        data = await resp.json()
        assert data["default"], "expected default icons"
        assert all(name.endswith(".webp") for name in data["default"])
        assert all(name.endswith(".webp") for name in data["custom"])

    async def test_js_served_gzipped_with_cache(self, portal_client):
        client, _ = portal_client
        resp = await client.get(
            "/static/js/icon-manager.js", headers={"Accept-Encoding": "gzip"}
        )
        assert resp.status == 200
        assert resp.headers["Content-Encoding"] == "gzip"
        assert "max-age" in resp.headers["Cache-Control"]
        assert resp.content_type == "text/javascript"

    async def test_image_has_long_cache_and_no_gzip(self, portal_client):
        client, _ = portal_client
        resp = await client.get(
            "/static/icons/sessions/fox.webp", headers={"Accept-Encoding": "gzip"}
        )
        assert resp.status == 200
        assert resp.headers.get("Content-Encoding") is None
        assert "max-age=604800" in resp.headers["Cache-Control"]
        assert resp.content_type == "image/webp"

    async def test_webp_served_with_bare_aiohttp_mime_db(self, portal_client):
        """Regression for #525: hermetic CI runners have a bare system mime DB,
        so aiohttp's private mimetypes instance doesn't know .webp and would
        serve octet-stream. The handler must pass Content-Type explicitly."""
        import aiohttp.web_fileresponse as fr

        client, _ = portal_client
        saved = {
            ext: fr.CONTENT_TYPES.types_map[1].pop(ext, None)
            for ext in (".webp",)
        }
        try:
            resp = await client.get("/static/icons/sessions/fox.webp")
            assert resp.status == 200
            assert resp.content_type == "image/webp"
        finally:
            for ext, ct in saved.items():
                if ct is not None:
                    fr.CONTENT_TYPES.types_map[1][ext] = ct

    async def test_no_gzip_without_accept_encoding(self, portal_client):
        client, _ = portal_client
        resp = await client.get(
            "/static/js/icon-manager.js", headers={"Accept-Encoding": "identity"}
        )
        assert resp.status == 200
        assert resp.headers.get("Content-Encoding") is None

    async def test_path_traversal_blocked(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/static/../server.py")
        assert resp.status == 404

    async def test_missing_static_returns_404(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/static/does-not-exist.js")
        assert resp.status == 404


class TestMonitorInProcessCapture:
    """#489 — the monitor captures session output IN-PROCESS via
    agent.get_output instead of spawning a per-session `agentwire output`
    subprocess, while still broadcasting dashboard activity for every session."""

    def _server(self, tmp_path):
        server = AgentWireServer(_make_config(tmp_path))
        server.agent = MagicMock()
        server.agent.get_output = MagicMock(return_value="scrollback")
        return server

    async def _run_one_tick(self, server):
        # Drive exactly one deterministic tick: awaiting _monitor_tick returns
        # only after every listed session's get_output has completed, so there
        # is no wall-clock sampling race (the old fixed-sleep + cancel approach
        # intermittently cancelled mid-tick before the 2nd session was polled).
        threshold = server.config.server.activity_threshold_seconds
        await server._monitor_tick({}, threshold)

    async def test_captures_in_process_no_output_subprocess(self, tmp_path):
        server = self._server(tmp_path)

        server._list_local_sessions = AsyncMock(
            return_value=[{"name": "alpha"}, {"name": "beta"}]
        )
        server._list_remote_sessions = AsyncMock(return_value={})
        server.run_agentwire_cmd = AsyncMock()
        server.broadcast_dashboard = AsyncMock()
        server.dashboard_clients.add(object())  # tick is skipped with no clients (#627)

        await self._run_one_tick(server)

        # Output captured in-process for every listed session...
        captured = {c.args[0] for c in server.agent.get_output.call_args_list}
        assert {"alpha", "beta"} <= captured
        # ...and NEVER via any CLI subprocess (listing or output — #627).
        server.run_agentwire_cmd.assert_not_awaited()

    async def test_dashboard_activity_broadcast_for_all_sessions(self, tmp_path):
        """A session with fresh output gets active:true on the dashboard,
        whether or not it has an open window (single source = the monitor)."""
        server = self._server(tmp_path)
        # Distinct output per session so each registers a change → active.
        server.agent.get_output = MagicMock(
            side_effect=lambda name, lines=50: f"output-for-{name}"
        )

        server._list_local_sessions = AsyncMock(
            return_value=[{"name": "watched"}, {"name": "headless"}]
        )
        server._list_remote_sessions = AsyncMock(return_value={})
        server.broadcast_dashboard = AsyncMock()

        # "watched" has an open window; "headless" does not. Both must broadcast.
        sess = Session(name="watched", config=await server._get_session_config("watched"))
        sess.clients.add(object())
        server.active_sessions["watched"] = sess

        await self._run_one_tick(server)

        active = {
            c.args[1]["session"]
            for c in server.broadcast_dashboard.call_args_list
            if c.args[0] == "session_activity" and c.args[1].get("active") is True
        }
        assert {"watched", "headless"} <= active


class TestMonitorLifecycle:
    """#627 — tick is skipped with no clients; #629 — active_sessions is
    reconciled against the live tmux list each tick."""

    def _server(self, tmp_path):
        server = AgentWireServer(_make_config(tmp_path))
        server.agent = MagicMock()
        server.agent.get_output = MagicMock(return_value="scrollback")
        server._list_local_sessions = AsyncMock(return_value=[{"name": "alive"}])
        server._list_remote_sessions = AsyncMock(return_value={})
        server.broadcast_dashboard = AsyncMock()
        return server

    async def _tick(self, server):
        await server._monitor_tick({}, server.config.server.activity_threshold_seconds)

    async def test_tick_skipped_with_no_clients(self, tmp_path):
        server = self._server(tmp_path)
        await self._tick(server)
        server._list_local_sessions.assert_not_awaited()
        server.agent.get_output.assert_not_called()

    async def test_evicts_vanished_session_and_closes_ws(self, tmp_path):
        server = self._server(tmp_path)
        server.dashboard_clients.add(object())

        dead = Session(name="dead", config=SessionConfig())
        dead.created_at = 0.0  # long past the grace window
        ws = AsyncMock()
        dead.clients.add(ws)
        server.active_sessions["dead"] = dead

        alive = Session(name="alive", config=SessionConfig())
        alive.created_at = 0.0
        server.active_sessions["alive"] = alive

        await self._tick(server)

        assert "dead" not in server.active_sessions
        assert "alive" in server.active_sessions
        # Clients are told the session truly ended BEFORE the close — a bare
        # close reads as a transient drop and the frontends auto-reconnect,
        # recreating the Session in an endless evict/reconnect cycle.
        ws.send_json.assert_awaited_with({"type": "local_session_ended", "session": "dead"})
        ws.close.assert_awaited()

    async def test_notify_session_created_invalidates_listing_caches(self, tmp_path):
        """#662 review: the sessions_update broadcast after a session_created
        notify must not serve a TTL-stale list that omits the new session."""
        import time as _time
        server = self._server(tmp_path)
        server._local_list_cache = (_time.monotonic(), [{"name": "old-only"}])
        server._remote_list_cache = (_time.monotonic(), {})

        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post(
                "/api/notify", json={"event": "session_created", "session": "foo"}
            )
            assert resp.status == 200
        assert server._local_list_cache is None
        assert server._remote_list_cache is None

    async def test_grace_window_protects_new_sessions(self, tmp_path):
        server = self._server(tmp_path)
        server.dashboard_clients.add(object())
        fresh = Session(name="starting", config=SessionConfig())
        server.active_sessions["starting"] = fresh  # created_at = now

        await self._tick(server)

        assert "starting" in server.active_sessions

    async def test_remote_session_kept_when_machine_unreachable(self, tmp_path):
        server = self._server(tmp_path)
        server.dashboard_clients.add(object())
        remote = Session(name="dev@pc", config=SessionConfig())
        remote.created_at = 0.0
        server.active_sessions["dev@pc"] = remote

        # Machine "pc" absent from the remote listing = unreachable → no verdict.
        await self._tick(server)
        assert "dev@pc" in server.active_sessions

        # Machine reachable and the session is gone → evict.
        server._list_remote_sessions = AsyncMock(return_value={"pc": []})
        await self._tick(server)
        assert "dev@pc" not in server.active_sessions

    async def test_dashboard_pseudo_session_never_evicted(self, tmp_path):
        server = self._server(tmp_path)
        server.dashboard_clients.add(object())
        dash = Session(name="dashboard", config=SessionConfig())
        dash.created_at = 0.0
        server.active_sessions["dashboard"] = dash

        await self._tick(server)
        assert "dashboard" in server.active_sessions


# ---------------------------------------------------------------------------
# Security response headers (CSP & friends)
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    async def test_headers_on_index(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert resp.headers["Referrer-Policy"] == "same-origin"
        csp = resp.headers["Content-Security-Policy"]
        assert "script-src 'self' https://cdn.jsdelivr.net" in csp
        assert "connect-src 'self' ws: wss: https://raw.githubusercontent.com" in csp
        # Plain-HTTP test transport: no HSTS.
        assert "Strict-Transport-Security" not in resp.headers

    async def test_headers_on_api_route(self, portal_client):
        client, _ = portal_client
        resp = await client.get("/api/artifacts")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in resp.headers

    async def test_artifacts_get_stricter_csp(self, portal_client, tmp_path):
        client, server = portal_client
        (server.config.artifacts.dir / "t.html").write_text("<h1>hi</h1>")
        resp = await client.get("/artifacts/t.html")
        assert resp.status == 200
        csp = resp.headers["Content-Security-Policy"]
        assert "frame-ancestors 'self'" in csp
        assert "object-src 'none'" in csp

    async def test_headers_on_error_response(self, portal_client_with_token):
        client, _ = portal_client_with_token
        resp = await client.get("/api/artifacts")  # 401: no token
        assert resp.status == 401
        assert resp.headers["X-Content-Type-Options"] == "nosniff"


# ---------------------------------------------------------------------------
# Bounded multipart uploads (413 before buffering)
# ---------------------------------------------------------------------------


class TestUploadSizeLimits:
    async def test_upload_over_limit_413(self, portal_client, tmp_path):
        client, server = portal_client
        server.config.uploads = type(server.config.uploads)(
            dir=tmp_path / "uploads", max_size_mb=1
        )
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("image", b"\x89PNG" + b"x" * (2 * 1024 * 1024),
                       filename="big.png", content_type="image/png")
        resp = await client.post("/upload", data=form)
        assert resp.status == 413

    async def test_upload_under_limit_succeeds(self, portal_client, tmp_path):
        client, server = portal_client
        server.config.uploads = type(server.config.uploads)(
            dir=tmp_path / "uploads", max_size_mb=1
        )
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("image", b"\x89PNGsmall",
                       filename="small.png", content_type="image/png")
        resp = await client.post("/upload", data=form)
        assert resp.status == 200
        data = await resp.json()
        assert data["filename"].endswith(".png")

    async def test_transcribe_over_limit_413(self, portal_client, tmp_path):
        client, server = portal_client
        server.config.uploads = type(server.config.uploads)(
            dir=tmp_path / "uploads", max_size_mb=1
        )
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("audio", b"a" * (2 * 1024 * 1024),
                       filename="rec.webm", content_type="audio/webm")
        resp = await client.post("/transcribe", data=form)
        assert resp.status == 413
