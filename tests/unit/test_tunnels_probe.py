"""Unit tests for the SSH/service probe trust model (issue #498, gap #3).

`test_ssh_connectivity` must not TOFU-accept unknown host keys, and
`test_service_health` must only disable TLS verification for loopback targets.
"""

import ssl

from agentwire import tunnels


class _Result:
    success = True


class TestSshConnectivityHostKeys:
    def test_rejects_unknown_host_keys(self, monkeypatch):
        captured = {}

        def fake_run_command(cmd, timeout=None):
            captured["cmd"] = cmd
            return _Result()

        monkeypatch.setattr(tunnels, "run_command", fake_run_command)
        tunnels.test_ssh_connectivity("example.com", user="me")

        cmd = captured["cmd"]
        # No silent trust-on-first-use of unknown host keys.
        assert "StrictHostKeyChecking=accept-new" not in cmd
        assert "StrictHostKeyChecking=yes" in cmd


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestServiceHealthTlsVerification:
    def test_loopback_skips_verification(self, monkeypatch):
        import urllib.request

        seen = {}

        def fake_urlopen(req, timeout=None, context=None):
            seen["ctx"] = context
            return _FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        ok, err = tunnels.test_service_health("https://127.0.0.1:9000/health")
        assert ok
        assert seen["ctx"].verify_mode == ssl.CERT_NONE
        assert seen["ctx"].check_hostname is False

    def test_remote_keeps_verification(self, monkeypatch):
        import urllib.request

        seen = {}

        def fake_urlopen(req, timeout=None, context=None):
            seen["ctx"] = context
            return _FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        ok, err = tunnels.test_service_health("https://example.com/health")
        assert ok
        # Remote target: full certificate + hostname verification, no CERT_NONE.
        assert seen["ctx"].verify_mode == ssl.CERT_REQUIRED
        assert seen["ctx"].check_hostname is True
