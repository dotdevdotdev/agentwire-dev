"""Unit tests for agentwire.security — origin policy, token lifecycle, bind policy."""

from types import SimpleNamespace

import pytest

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
    def _config(self, tmp_path, host, auth_token):
        config = load_config(tmp_path / "nonexistent.yaml")
        config.server.host = host
        config.server.auth_token = auth_token
        return config

    def test_non_loopback_without_token_refuses(self, tmp_path):
        config = self._config(tmp_path, "0.0.0.0", None)
        with pytest.raises(SystemExit):
            security.validate_startup_security(config)

    def test_non_loopback_with_token_passes(self, tmp_path):
        config = self._config(tmp_path, "0.0.0.0", "tok")
        security.validate_startup_security(config)

    def test_loopback_without_token_passes(self, tmp_path):
        config = self._config(tmp_path, "127.0.0.1", None)
        security.validate_startup_security(config)
