"""Unit tests for agentwire.security — origin policy, token lifecycle, bind policy."""

import os
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

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "myhost.lan"])
    def test_non_loopback(self, host):
        assert security.is_loopback_host(host) is False


# ---------------------------------------------------------------------------
# Origin policy
# ---------------------------------------------------------------------------


def _request(scheme="https", host="192.168.2.10:8765"):
    return SimpleNamespace(scheme=scheme, host=host)


class TestOriginAllowed:
    def test_own_origin(self):
        assert security.origin_allowed(
            "https://192.168.2.10:8765", _request(), []
        )

    def test_evil_origin_rejected(self):
        assert not security.origin_allowed("https://evil.example", _request(), [])

    def test_allowed_origins_entry(self):
        # Cloudflare Tunnel: public https origin, portal itself plain http
        assert security.origin_allowed(
            "https://portal.example.com",
            _request(scheme="http", host="127.0.0.1:8765"),
            ["https://portal.example.com"],
        )

    def test_localhost_equivalents_same_port(self):
        # Browser at https://localhost:8765 posting to 127.0.0.1:8765
        assert security.origin_allowed(
            "https://localhost:8765", _request(host="127.0.0.1:8765"), []
        )

    def test_localhost_wrong_port_rejected(self):
        assert not security.origin_allowed(
            "https://localhost:9999", _request(host="127.0.0.1:8765"), []
        )

    def test_localhost_origin_to_lan_host_rejected(self):
        # Origin is loopback but the portal is reached via a LAN IP — a page
        # on the *remote* user's machine shouldn't pass as same-site.
        assert not security.origin_allowed(
            "https://localhost:8765", _request(host="192.168.2.10:8765"), []
        )

    def test_default_port_normalization(self):
        assert security.origin_allowed(
            "https://localhost", _request(host="127.0.0.1:443"), []
        )


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "portal.token"
    monkeypatch.setattr(security, "TOKEN_FILE", path)
    return path


class TestTokenLifecycle:
    def _config(self, tmp_path, auth_token=None):
        config = load_config(tmp_path / "nonexistent.yaml")
        config.server.auth_token = auth_token
        return config

    def test_generate_token_length(self):
        token = security.generate_token()
        assert len(token) >= 32

    def test_ensure_generates_file(self, token_file, tmp_path):
        config = self._config(tmp_path)
        token = security.ensure_auth_token(config)
        assert token
        assert token_file.read_text().strip() == token
        assert (token_file.stat().st_mode & 0o777) == 0o600

    def test_ensure_respects_existing_file(self, token_file, tmp_path):
        token_file.write_text("existing-token\n")
        config = self._config(tmp_path)
        assert security.ensure_auth_token(config) == "existing-token"

    def test_config_override_wins(self, token_file, tmp_path):
        token_file.write_text("file-token\n")
        config = self._config(tmp_path, auth_token="override-token")
        assert security.ensure_auth_token(config) == "override-token"
        # Override must not rewrite the file
        assert token_file.read_text().strip() == "file-token"

    def test_explicit_disable(self, token_file, tmp_path):
        token_file.write_text("file-token\n")
        config = self._config(tmp_path, auth_token="")
        assert security.ensure_auth_token(config) is None
        assert security.resolve_auth_token(config) is None

    def test_read_missing_file(self, token_file):
        assert security.read_token_file() is None


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

    def test_non_loopback_plaintext_refuses(self, tmp_path):
        # Token set, but no TLS and no opt-in: token would transit in cleartext.
        config = self._config(tmp_path, "0.0.0.0", "tok")
        with pytest.raises(SystemExit, match="cleartext"):
            security.validate_startup_security(config)

    def test_non_loopback_plaintext_with_optin_passes(self, tmp_path):
        config = self._config(tmp_path, "0.0.0.0", "tok", allow_insecure=True)
        security.validate_startup_security(config)

    def test_loopback_without_token_passes(self, tmp_path):
        config = self._config(tmp_path, "127.0.0.1", None)
        security.validate_startup_security(config)

    def test_loopback_plaintext_passes(self, tmp_path):
        # Local dev: plaintext loopback bind must never be refused.
        config = self._config(tmp_path, "127.0.0.1", "tok")
        security.validate_startup_security(config)


# ---------------------------------------------------------------------------
# Auth-failure tracking + lockout (gap #5)
# ---------------------------------------------------------------------------


class TestAuthFailureTracker:
    def test_records_and_counts_within_window(self):
        t = security.AuthFailureTracker(threshold=3, window=100.0)
        assert t.record("1.2.3.4", now=0.0) == 1
        assert t.record("1.2.3.4", now=1.0) == 2
        assert not t.is_locked("1.2.3.4", now=1.0)
        assert t.record("1.2.3.4", now=2.0) == 3
        assert t.is_locked("1.2.3.4", now=2.0)

    def test_window_slides(self):
        t = security.AuthFailureTracker(threshold=2, window=100.0)
        t.record("1.2.3.4", now=0.0)
        t.record("1.2.3.4", now=1.0)
        assert t.is_locked("1.2.3.4", now=1.0)
        # Old failures age out of the window.
        assert not t.is_locked("1.2.3.4", now=200.0)

    def test_per_ip_isolation(self):
        t = security.AuthFailureTracker(threshold=2, window=100.0)
        t.record("1.1.1.1", now=0.0)
        t.record("1.1.1.1", now=1.0)
        assert t.is_locked("1.1.1.1", now=1.0)
        assert not t.is_locked("2.2.2.2", now=1.0)

    def test_empty_buckets_pruned(self):
        t = security.AuthFailureTracker(threshold=5, window=10.0)
        t.record("1.1.1.1", now=0.0)
        # A later record on a different IP prunes the now-stale first bucket.
        t.record("2.2.2.2", now=100.0)
        assert "1.1.1.1" not in t._fails


# ---------------------------------------------------------------------------
# Frozen security-critical config (#425)
# ---------------------------------------------------------------------------


class TestFrozenConfig:
    OLD = (
        'server:\n'
        '  host: "127.0.0.1"\n'
        '  port: 8765\n'
        '  auth_token: "realsecret"\n'
        'executables:\n'
        '  claude: "/usr/bin/claude"\n'
        'safety:\n'
        '  enabled: true\n'
    )

    def test_unchanged_config_allowed(self):
        assert security.frozen_config_violations(self.OLD, self.OLD) == []

    def test_changing_auth_token_blocked(self):
        new = self.OLD.replace('"realsecret"', '""')
        assert "server.auth_token" in security.frozen_config_violations(new, self.OLD)

    def test_changing_host_blocked(self):
        new = self.OLD.replace('"127.0.0.1"', '"0.0.0.0"')
        assert "server.host" in security.frozen_config_violations(new, self.OLD)

    def test_changing_executables_blocked(self):
        new = self.OLD.replace('/usr/bin/claude', '/tmp/evil')
        assert "executables" in security.frozen_config_violations(new, self.OLD)

    def test_disabling_safety_blocked(self):
        new = self.OLD.replace("enabled: true", "enabled: false")
        assert "safety" in security.frozen_config_violations(new, self.OLD)

    def test_non_frozen_change_allowed(self):
        new = self.OLD.replace("port: 8765", "port: 9000")
        assert security.frozen_config_violations(new, self.OLD) == []

    def test_redacted_auth_token_is_not_a_change(self):
        # The UI round-trips auth_token as "[REDACTED]"; restoring it first means
        # an otherwise-unchanged save must not trip the frozen check.
        redacted = self.OLD.replace('"realsecret"', '"[REDACTED]"')
        restored = security.restore_redactions(redacted, self.OLD)
        assert "[REDACTED]" not in restored
        assert "realsecret" in restored
        assert security.frozen_config_violations(restored, self.OLD) == []

    def test_redaction_restores_each_secret_by_path(self):
        # S1 (PR #458 red-team): multiple api_key entries at different paths must
        # each get their OWN secret back — not a single global first-match.
        old = (
            "pi:\n"
            "  providers:\n"
            "    zai:\n"
            '      api_key: "zai-secret"\n'
            "    openai:\n"
            '      api_key: "openai-secret"\n'
            "server:\n"
            '  auth_token: "tok"\n'
        )
        redacted = (
            "pi:\n"
            "  providers:\n"
            "    zai:\n"
            '      api_key: "[REDACTED]"\n'
            "    openai:\n"
            '      api_key: "[REDACTED]"\n'
            "server:\n"
            '  auth_token: "[REDACTED]"\n'
        )
        restored = security.restore_redactions(redacted, old)
        assert "[REDACTED]" not in restored
        # Each key restored to its own path's value, in order.
        assert restored.index("zai-secret") < restored.index("openai-secret")
        assert "tok" in restored
        # Comments and unrelated lines survive (text round-trip, not re-serialize).
        assert restored.count("api_key") == 2

    def test_redaction_preserves_comments(self):
        old = 'server:\n  auth_token: "realsecret"\n  port: 8765\n'
        redacted = (
            "# my portal config\n"
            "server:\n"
            '  auth_token: "[REDACTED]"\n'
            "  port: 8765  # keep this comment\n"
        )
        restored = security.restore_redactions(redacted, old)
        assert "# my portal config" in restored
        assert "# keep this comment" in restored
        assert "realsecret" in restored


# ---------------------------------------------------------------------------
# Token → device resolution
# ---------------------------------------------------------------------------


class TestResolveDevice:
    def test_bootstrap_token_resolves(self):
        dev = security.resolve_device("boot", auth_token="boot")
        assert dev is security.BOOTSTRAP_DEVICE

    def test_wrong_bootstrap_and_empty_registry_is_none(self):
        assert security.resolve_device("nope", auth_token="boot") is None

    def test_paired_token_resolves(self, monkeypatch):
        from agentwire.devices import DeviceRegistry

        reg = DeviceRegistry.load()  # path patched to tmp by autouse fixture
        device, token = reg.add("phone")
        from agentwire import devices as devices_mod

        devices_mod._cache.clear()
        resolved = security.resolve_device(token, auth_token="boot")
        assert resolved is not None
        assert resolved.id == device.id


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

    async def test_loopback_never_locked_out(self, monkeypatch):
        monkeypatch.setattr(security, "resolve_device", lambda *a, **k: None)
        locked = []
        mw = security.create_security_middleware(
            "secret", [], on_lockout=lambda ip, n: locked.append(ip)
        )
        # Far more than the threshold from loopback: always 401, never 429.
        for _ in range(security.AUTH_FAIL_LOCKOUT + 5):
            with pytest.raises(web.HTTPUnauthorized):
                await mw(self._request("127.0.0.1"), self._handler)
        assert locked == []


# ---------------------------------------------------------------------------
# Token file hardening (atomic 0600 write, 0700 config dir)


class TestWriteTokenFile:
    def test_new_file_is_0600_and_dir_0700(self, tmp_path, monkeypatch):
        path = tmp_path / "aw" / "portal.token"
        monkeypatch.setattr(security, "TOKEN_FILE", path)
        old_umask = os.umask(0o000)  # worst-case permissive umask
        try:
            security.write_token_file("tok-1")
        finally:
            os.umask(old_umask)
        assert path.read_text() == "tok-1\n"
        assert (path.stat().st_mode & 0o777) == 0o600
        assert (path.parent.stat().st_mode & 0o777) == 0o700

    def test_rotate_over_existing_stays_0600(self, tmp_path, monkeypatch):
        path = tmp_path / "portal.token"
        monkeypatch.setattr(security, "TOKEN_FILE", path)
        security.write_token_file("tok-1")
        security.write_token_file("tok-2")
        assert path.read_text() == "tok-2\n"
        assert (path.stat().st_mode & 0o777) == 0o600
        # No leftover temp files from the atomic write.
        assert list(path.parent.glob(".portal.token.*")) == []


# ---------------------------------------------------------------------------
# Security response headers


class TestSecurityHeaders:
    def test_csp_covers_ui_requirements(self):
        csp = security._build_csp()
        assert "script-src 'self' https://cdn.jsdelivr.net" in csp
        assert "ws: wss:" in csp
        assert "https://raw.githubusercontent.com" in csp
        assert "frame-ancestors 'self'" in csp
        # pair.html's inline module must be hash-allowed, not broken.
        assert "'sha256-" in csp

    def test_inline_script_hashes_found(self):
        # pair.html ships an inline <script type="module"> block.
        assert security._inline_script_hashes()

    @staticmethod
    def _stamp(secure: bool, hsts_enabled: bool, path: str = "/"):
        request = SimpleNamespace(secure=secure, path=path)
        response = SimpleNamespace(headers={})
        security._stamp_security_headers(request, response, "default-csp", hsts_enabled)
        return response.headers

    def test_hsts_off_by_default_even_on_secure_request(self):
        headers = self._stamp(secure=True, hsts_enabled=False)
        assert "Strict-Transport-Security" not in headers

    def test_hsts_emitted_when_enabled_and_secure(self):
        headers = self._stamp(secure=True, hsts_enabled=True)
        assert headers["Strict-Transport-Security"] == "max-age=31536000"

    def test_hsts_never_on_plain_http(self):
        headers = self._stamp(secure=False, hsts_enabled=True)
        assert "Strict-Transport-Security" not in headers

    def test_other_headers_unchanged_by_hsts_gate(self):
        headers = self._stamp(secure=True, hsts_enabled=False)
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "SAMEORIGIN"
        assert headers["Referrer-Policy"] == "same-origin"
        assert headers["Content-Security-Policy"] == "default-csp"

    def test_artifacts_csp_unaffected(self):
        headers = self._stamp(secure=True, hsts_enabled=True, path="/artifacts/x.html")
        assert headers["Content-Security-Policy"] == security._ARTIFACTS_CSP

    def test_server_hsts_config_defaults_false(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.server.hsts is False


# ---------------------------------------------------------------------------
# Bounded multipart reads


class _FakeField:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read_chunk(self, size=None):
        return self._chunks.pop(0) if self._chunks else b""


class TestReadMultipartFieldLimited:
    async def test_under_limit_returns_bytes(self):
        field = _FakeField([b"aa", b"bb"])
        assert await security.read_multipart_field_limited(field, 10) == b"aabb"

    async def test_over_limit_raises_413_before_buffering_rest(self):
        field = _FakeField([b"x" * 8, b"y" * 8, b"z" * 8])
        with pytest.raises(web.HTTPRequestEntityTooLarge):
            await security.read_multipart_field_limited(field, 10)
        # The third chunk was never consumed — we aborted mid-stream.
        assert field._chunks == [b"z" * 8]
