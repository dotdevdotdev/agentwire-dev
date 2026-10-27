"""Unit tests for the SSH/service probe trust model (issue #498, gap #3).

`test_ssh_connectivity` must not TOFU-accept unknown host keys, and
`test_service_health` must only disable TLS verification for loopback targets.
"""

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
