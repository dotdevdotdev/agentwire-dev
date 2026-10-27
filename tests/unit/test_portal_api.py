"""Integration tests for portal API handlers via aiohttp TestClient."""

from unittest.mock import AsyncMock, patch

import pytest
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



def _make_server(config):
    """An AgentWireServer whose tmux pane probe is stubbed.

    ``_wait_for_pane_ready`` polls a real ``tmux capture-pane`` for up to 2 s;
    with no such session every create-session test paid the full timeout (and
    touched the host's tmux). These tests assert the HTTP contract, not tmux.
    """
    server = AgentWireServer(config)
    server._wait_for_pane_ready = AsyncMock(return_value=True)
    return server

@pytest.fixture
async def portal_client(tmp_path):
    """Create an AgentWireServer and wrap in TestClient."""
    server = _make_server(_make_config(tmp_path))
    async with TestClient(TestServer(server.app)) as client:
        yield client, server


@pytest.fixture
async def portal_client_with_token(tmp_path):
    """Portal with bearer-token auth enforced."""
    server = _make_server(_make_config(tmp_path, auth_token="testtoken123"))
    async with TestClient(TestServer(server.app)) as client:
        yield client, server


@pytest.fixture
async def portal_client_with_origins(tmp_path):
    """Portal with an allowed_origins entry (tunnel-domain case)."""
    server = _make_server(
        _make_config(tmp_path, allowed_origins=["https://portal.example.com"])
    )
    async with TestClient(TestServer(server.app)) as client:
        yield client, server


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:


    async def test_health_reports_the_real_package_version(self, portal_client):
        """server.py used to hardcode its own __version__ = "1.3.0",
        disconnected from agentwire/__init__.py and stale since 2026-02 --
        /health silently lied about what was actually running. Pin against
        the real SSOT so a reintroduced hardcode fails this test."""
        from agentwire import __version__ as real_version

        client, _ = portal_client
        resp = await client.get("/health")
        data = await resp.json()
        assert data["version"] == real_version




# ---------------------------------------------------------------------------
# Sessions API
# ---------------------------------------------------------------------------




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










    async def test_create_first_message_stuck_draft_warns_not_calm_queued_toast(self, portal_client):
        """#843: an "inbox_stuck" fallback (queued, but the stale draft in
        the input box could not be confirmed cleared) must NOT reuse the
        calm "queued for guaranteed delivery" toast the plain "inbox" case
        gets -- the original draft may still be sitting there."""
        import asyncio

        client, server = portal_client

        async def cmd_router(args, json_output=True):
            if args[0] == "send":
                return (False, {"error": "Delivery not verified", "fallback": "inbox_stuck"})
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
            assert "paste it manually" not in toasts[0]["text"]
            assert "queued for guaranteed delivery" not in toasts[0]["text"]
            assert "could not be confirmed cleared" in toasts[0]["text"]



# ---------------------------------------------------------------------------
# Active-session shadow file API
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Close session API
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Voices API
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Artifacts API
# ---------------------------------------------------------------------------




class TestApiArtifactDownload:
    """GET /api/artifacts/download/{path} — attachment download (#707)."""



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





# ---------------------------------------------------------------------------
# Desktop windows API
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Notify API
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Security middleware: Origin validation (CSRF guard)
# ---------------------------------------------------------------------------


class TestOriginValidation:


    async def test_post_evil_origin_rejected(self, portal_client):
        client, _ = portal_client
        resp = await client.post(
            "/api/notify", json={"event": "x"},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status == 403





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



# ---------------------------------------------------------------------------
# /mobile page (#279 — phone PTT surface)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Session state computation (#290 — mobile state visuals)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Artifact upload/delete path safety
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# /api/voice-status
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Default-tier managed STT shim lifecycle (ensure_managed_stt)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# CLI overrides (--no-stt / --no-tts) — apply_cli_overrides
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Default-tier managed Kokoro TTS shim lifecycle (ensure_managed_tts)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Custom-tier voices cache (#271 — unreachable shim must not stall page load)
# ---------------------------------------------------------------------------




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





class TestPerDeviceAuth:

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





# ---------------------------------------------------------------------------
# Static assets: WebP icons, gzip, Cache-Control (#488)
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Security response headers (CSP & friends)
# ---------------------------------------------------------------------------


class TestSecurityHeaders:


    async def test_artifacts_get_stricter_csp(self, portal_client, tmp_path):
        client, server = portal_client
        (server.config.artifacts.dir / "t.html").write_text("<h1>hi</h1>")
        resp = await client.get("/artifacts/t.html")
        assert resp.status == 200
        csp = resp.headers["Content-Security-Policy"]
        assert "frame-ancestors 'self'" in csp
        assert "object-src 'none'" in csp



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


