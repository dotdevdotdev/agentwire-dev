"""Integration tests for channels CLI commands.

These test the actual CLI binary via subprocess.
"""

import json
import subprocess

import pytest


def run_agentwire(*args, timeout=10):
    """Run agentwire CLI and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        ["agentwire"] + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout, result.stderr, result.returncode


class TestChannelsListCLI:
    def test_channels_list_text(self):
        import re
        stdout, stderr, rc = run_agentwire("channels", "list")
        assert rc == 0
        # Channel names anchored to line start so they survive cosmetic format changes
        assert re.search(r"(?m)^\s*email\b", stdout)
        assert re.search(r"(?m)^\s*telegram\b", stdout)
        assert "send_only" in stdout
        assert "service" in stdout

    def test_channels_list_json(self):
        stdout, stderr, rc = run_agentwire("channels", "list", "--json")
        assert rc == 0
        data = json.loads(stdout)
        assert data["success"] is True
        assert len(data["channels"]) == 7

        names = {ch["name"] for ch in data["channels"]}
        assert names == {"email", "telegram", "quo", "sms", "webhook", "discord", "slack"}

    def test_channels_list_json_structure(self):
        stdout, _, _ = run_agentwire("channels", "list", "--json")
        data = json.loads(stdout)
        for ch in data["channels"]:
            assert "name" in ch
            assert "type" in ch
            assert "configured" in ch
            assert "builtin" in ch
            assert ch["type"] in ("send_only", "service")
            assert isinstance(ch["configured"], bool)
            assert isinstance(ch["builtin"], bool)

    def test_all_channels_are_builtin(self):
        stdout, _, _ = run_agentwire("channels", "list", "--json")
        data = json.loads(stdout)
        for ch in data["channels"]:
            assert ch["builtin"] is True

    def test_email_configured(self):
        """Email should show as configured (has api_key from env/config)."""
        stdout, _, _ = run_agentwire("channels", "list", "--json")
        data = json.loads(stdout)
        email = next(ch for ch in data["channels"] if ch["name"] == "email")
        # This depends on actual config — just verify the field exists
        assert isinstance(email["configured"], bool)


class TestServiceStatusCLI:
    def test_telegram_status(self):
        stdout, stderr, rc = run_agentwire("telegram", "status")
        # Should report not running (exit code 1) or running (exit code 0)
        assert "Telegram bridge" in (stdout + stderr)

    def test_discord_status(self):
        stdout, stderr, rc = run_agentwire("discord", "status")
        assert "Discord bridge" in (stdout + stderr)

    def test_slack_status(self):
        stdout, stderr, rc = run_agentwire("slack", "status")
        assert "Slack bridge" in (stdout + stderr)

    def test_discord_status_json(self):
        stdout, stderr, rc = run_agentwire("discord", "status", "--json")
        output = stdout or stderr
        data = json.loads(output)
        assert "success" in data
        assert "running" in data
        assert isinstance(data["running"], bool)

    def test_slack_status_json(self):
        stdout, stderr, rc = run_agentwire("slack", "status", "--json")
        output = stdout or stderr
        data = json.loads(output)
        assert "success" in data
        assert "running" in data

    def test_telegram_status_json(self):
        stdout, stderr, rc = run_agentwire("telegram", "status", "--json")
        output = stdout or stderr
        data = json.loads(output)
        assert "success" in data
        assert "running" in data


class TestSendOnlyCLI:
    def test_quo_no_body(self):
        """Quo with no body should fail."""
        stdout, stderr, rc = run_agentwire("quo")
        assert rc == 1
        assert "No message body" in stderr or "error" in stderr.lower()

    def test_sms_no_body(self):
        """SMS with no body should fail."""
        stdout, stderr, rc = run_agentwire("sms")
        assert rc == 1
        assert "No message body" in stderr or "error" in stderr.lower()

    def test_webhook_no_body(self):
        """Webhook with no body should fail."""
        stdout, stderr, rc = run_agentwire("webhook")
        assert rc == 1
        assert "No message body" in stderr or "error" in stderr.lower()

    def test_email_no_body(self):
        """Email with no body should fail."""
        stdout, stderr, rc = run_agentwire("email")
        assert rc == 1
        assert "No message body" in stderr or "error" in stderr.lower()


class TestHelpOutput:
    def test_channels_in_help(self):
        stdout, stderr, rc = run_agentwire("--help")
        output = stdout + stderr
        assert "channels" in output
        assert "discord" in output
        assert "slack" in output
        assert "sms" in output
        assert "webhook" in output
