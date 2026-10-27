"""Unit tests for agentwire.security — origin policy, token lifecycle, bind policy."""

from types import SimpleNamespace

import pytest
from aiohttp import web

from agentwire import security
from agentwire.config import load_config

# ---------------------------------------------------------------------------
# Loopback detection
# ---------------------------------------------------------------------------


class TestIsLoopbackHost:
    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.5"])
    def test_loopback(self, host):
        assert security.is_loopback_host(host) is True


# ---------------------------------------------------------------------------
# Origin policy
# ---------------------------------------------------------------------------


def _request(scheme="https", host="192.168.2.10:8765"):
    return SimpleNamespace(scheme=scheme, host=host)


class TestOriginAllowed:
    def test_evil_origin_rejected(self):
        assert not security.origin_allowed("https://evil.example", _request(), [])


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Startup guard
# ---------------------------------------------------------------------------


class TestValidateStartupSecurity:
    def _config(self, tmp_path, host, auth_token, allow_insecure=False):
        config = load_config(tmp_path / "nonexistent.yaml")
        config.server.host = host
        config.server.auth_token = auth_token
        config.server.allow_insecure = allow_insecure
        return config

    def test_non_loopback_without_token_refuses(self, tmp_path):
        config = self._config(tmp_path, "0.0.0.0", None)
        with pytest.raises(SystemExit):
            security.validate_startup_security(config)


# ---------------------------------------------------------------------------
# Auth-failure tracking + lockout (gap #5)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Frozen security-critical config (#425)
# ---------------------------------------------------------------------------


class TestFrozenConfig:
    OLD = (
        "server:\n"
        '  host: "127.0.0.1"\n'
        "  port: 8765\n"
        '  auth_token: "realsecret"\n'
        "executables:\n"
        '  claude: "/usr/bin/claude"\n'
        "safety:\n"
        "  enabled: true\n"
    )

    def test_disabling_safety_blocked(self):
        new = self.OLD.replace("enabled: true", "enabled: false")
        assert "safety" in security.frozen_config_violations(new, self.OLD)


# ---------------------------------------------------------------------------
# Token → device resolution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Middleware lockout behaviour (gap #5)
# ---------------------------------------------------------------------------


class TestMiddlewareLockout:
    """Drive the middleware directly: bad tokens log, count, and eventually 429."""

    def _request(self, remote, token="badtoken"):
        # Minimal stand-in: the middleware only reads method/path/headers/remote
        # and item-assigns request["device"] on success (never hit here).
        class _Req(dict):
            pass

        req = _Req()
        req.method = "GET"
        req.path = "/api/sessions"
        req.headers = {"Authorization": f"Bearer {token}"}
        req.remote = remote
        return req

    async def _handler(self, request):
        return web.Response(text="ok")

    async def test_spray_locks_out_and_notifies(self, monkeypatch):
        monkeypatch.setattr(security, "resolve_device", lambda *a, **k: None)
        locked = []
        mw = security.create_security_middleware(
            "secret", [], on_lockout=lambda ip, n: locked.append((ip, n))
        )
        # First AUTH_FAIL_LOCKOUT-1 failures are 401; the threshold crossing is 429.
        for _ in range(security.AUTH_FAIL_LOCKOUT - 1):
            with pytest.raises(web.HTTPUnauthorized):
                await mw(self._request("9.9.9.9"), self._handler)
        with pytest.raises(web.HTTPTooManyRequests):
            await mw(self._request("9.9.9.9"), self._handler)
        assert locked == [("9.9.9.9", security.AUTH_FAIL_LOCKOUT)]
        # Subsequent requests stay locked out (429) without re-notifying.
        with pytest.raises(web.HTTPTooManyRequests):
            await mw(self._request("9.9.9.9"), self._handler)
        assert len(locked) == 1


# ---------------------------------------------------------------------------
# Token file hardening (atomic 0600 write, 0700 config dir)


# ---------------------------------------------------------------------------
# Security response headers


# ---------------------------------------------------------------------------
# Bounded multipart reads
