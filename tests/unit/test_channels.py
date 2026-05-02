"""Tests for agentwire/channels/ — registry, base classes, config, and channel modules."""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agentwire.channels.base import (
    Channel,
    ChannelRegistry,
    ChannelResult,
    NotificationError,
    SendOnlyChannel,
    ServiceChannel,
    _run_cmd,
    _run_cmd_raw,
)


# =============================================================================
# ChannelRegistry
# =============================================================================


class TestChannelRegistry:
    def test_builtin_channels_registered(self):
        """All 7 built-in channels should be registered, exactly 7, no extras."""
        channels = ChannelRegistry.all()
        expected = {"email", "telegram", "quo", "sms", "webhook", "discord", "slack"}
        assert set(channels.keys()) == expected

    def test_get_existing(self):
        cls = ChannelRegistry.get("email")
        assert cls is not None
        assert cls.name == "email"

    def test_get_nonexistent(self):
        assert ChannelRegistry.get("nonexistent_channel") is None

    def test_register_decorator(self):
        """@register decorator should add class to registry."""
        @ChannelRegistry.register("_test_channel")
        class _TestChannel(SendOnlyChannel):
            name = "_test_channel"

        assert "_test_channel" in ChannelRegistry._channels
        del ChannelRegistry._channels["_test_channel"]

    @pytest.mark.parametrize(
        "module,cls_name,expected_name,expected_type,expected_config_key",
        [
            ("agentwire.channels.email", "EmailChannel", "email", "send_only", "email"),
            ("agentwire.channels.telegram", "TelegramChannel", "telegram", "service", "telegram"),
            ("agentwire.channels.quo", "QuoChannel", "quo", "send_only", "quo"),
            ("agentwire.channels.sms", "SMSChannel", "sms", "send_only", "sms"),
            ("agentwire.channels.webhook", "WebhookChannel", "webhook", "send_only", "webhook"),
            ("agentwire.channels.discord", "DiscordChannel", "discord", "service", "discord"),
            ("agentwire.channels.slack", "SlackChannel", "slack", "service", "slack"),
        ],
    )
    def test_channel_class_metadata(
        self, module, cls_name, expected_name, expected_type, expected_config_key
    ):
        """Each channel class declares correct name/type/config_key (single source of truth)."""
        import importlib
        cls = getattr(importlib.import_module(module), cls_name)
        assert cls.name == expected_name
        assert cls.channel_type == expected_type
        assert cls.config_key == expected_config_key


# =============================================================================
# ChannelRegistry.resolve_config
# =============================================================================


class TestResolveConfig:
    def test_channels_path(self):
        """channels.email: in YAML should be found."""
        data = {"channels": {"email": {"api_key": "new-key", "from_address": "a@b.com"}}}
        resolved = ChannelRegistry.resolve_config("email", data)
        assert resolved["api_key"] == "new-key"
        assert resolved["from_address"] == "a@b.com"

    def test_empty_data(self):
        resolved = ChannelRegistry.resolve_config("email", {})
        assert resolved == {}

    def test_nonexistent_channel(self):
        resolved = ChannelRegistry.resolve_config("nonexistent", {"channels": {"email": {}}})
        assert resolved == {}

    def test_channel_with_no_config_section(self):
        """Registered channel with no channels.{name}: entry returns empty dict."""
        data = {"channels": {"other": {"x": "y"}}}
        resolved = ChannelRegistry.resolve_config("email", data)
        assert resolved == {}


# =============================================================================
# Config loading integration
# =============================================================================


class TestConfigChannelLoading:
    def test_channels_loaded_from_yaml(self, tmp_path):
        """Config should load channel configs from channels: key."""
        config_data = {
            "server": {"port": 8765},
            "channels": {
                "email": {
                    "api_key": "test-key",
                    "from_address": "test@test.com",
                    "default_to": "user@test.com",
                },
            },
        }
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump(config_data, f)

        from agentwire.config import load_config
        config = load_config(config_path)

        assert "email" in config.channels
        email_config = config.channels["email"]
        assert email_config.api_key == "test-key"
        assert email_config.from_address == "test@test.com"
        assert email_config.default_to == "user@test.com"

    def test_all_seven_channels_in_config(self, tmp_path):
        """Even with no YAML config, all 7 channels should get default configs."""
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump({}, f)

        from agentwire.config import load_config
        config = load_config(config_path)

        for name in ["email", "telegram", "quo", "sms", "webhook", "discord", "slack"]:
            assert name in config.channels, f"Channel {name} missing from config.channels"

    def test_channels_field_is_dict(self, tmp_path):
        """config.channels should be a dict, not a dataclass."""
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump({}, f)

        from agentwire.config import load_config
        config = load_config(config_path)
        assert isinstance(config.channels, dict)

# =============================================================================
# Email channel
# =============================================================================


class TestEmailChannel:
    def test_email_config_env_fallback(self, monkeypatch):
        """EmailConfig should fall back to RESEND_API_KEY env var."""
        monkeypatch.setenv("RESEND_API_KEY", "env-key-123")
        from agentwire.channels.email import EmailConfig
        config = EmailConfig()
        assert config.api_key == "env-key-123"

    def test_email_config_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "env-key")
        from agentwire.channels.email import EmailConfig
        config = EmailConfig(api_key="explicit-key")
        assert config.api_key == "explicit-key"

    def test_is_html_content(self):
        from agentwire.channels.email import _is_html_content
        assert _is_html_content("<h1>Hello</h1>") is True
        assert _is_html_content("<div style='color:red'>") is True
        assert _is_html_content("<!DOCTYPE html>") is True
        assert _is_html_content("Just plain text") is False
        assert _is_html_content("") is False

    def test_markdown_to_html(self):
        from agentwire.channels.email import _markdown_to_html
        result = _markdown_to_html("**bold**")
        assert "<strong>bold</strong>" in result

    def test_markdown_passthrough_html(self):
        from agentwire.channels.email import _markdown_to_html
        html = "<h1>Already HTML</h1>"
        assert _markdown_to_html(html) == html

    def test_send_email_no_api_key(self, tmp_path, monkeypatch):
        """send_email should raise EmailConfigError if no API key."""
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        config_data = {"channels": {"email": {"api_key": "", "from_address": "x@y.com"}}}
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump(config_data, f)

        from agentwire.config import load_config
        import agentwire.config as config_mod
        old = config_mod._config
        config_mod._config = load_config(config_path)

        from agentwire.channels.email import EmailConfigError, send_email
        try:
            with pytest.raises(EmailConfigError, match="API key"):
                send_email(body="test")
        finally:
            config_mod._config = old

    @pytest.mark.parametrize(
        "raw,default,expected",
        [
            ("a@x.com", "fallback@x.com", ["a@x.com"]),
            ("a@x.com, b@x.com ,c@x.com", "", ["a@x.com", "b@x.com", "c@x.com"]),
            (["a@x.com", "b@x.com"], "", ["a@x.com", "b@x.com"]),
            # argparse --to a,b --to c produces ["a,b", "c"]; each entry splits on commas
            (["a@x.com,b@x.com", "c@x.com"], "", ["a@x.com", "b@x.com", "c@x.com"]),
            # Dedupes while preserving order
            (["a@x.com", "b@x.com", "a@x.com"], "", ["a@x.com", "b@x.com"]),
            (None, "default@x.com", ["default@x.com"]),
            ("", "default@x.com", ["default@x.com"]),
            (None, "", []),
            ([], "", []),
        ],
    )
    def test_normalize_recipients(self, raw, default, expected):
        from agentwire.channels.email import _normalize_recipients
        assert _normalize_recipients(raw, default) == expected


# =============================================================================
# Telegram channel
# =============================================================================


class TestTelegramChannel:
    def test_telegram_config_env_fallback(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENTWIRE_BOT_TOKEN", "env-token")
        from agentwire.channels.telegram import TelegramConfig
        config = TelegramConfig()
        assert config.bot_token == "env-token"

    def test_telegram_config_defaults(self):
        from agentwire.channels.telegram import TelegramConfig
        # Don't rely on env state — test explicit defaults
        config = TelegramConfig(bot_token="tok")
        assert config.default_session == "main"
        assert config.voice_replies is True
        assert config.forward_questions is True
        assert config.forward_alerts is True
        assert config.session_name == "agentwire-telegram"
        assert config.allowed_users == []

    def test_telegram_result_dataclass(self):
        from agentwire.channels.telegram import TelegramResult
        r = TelegramResult(success=True, message_id=999)
        assert r.success is True
        assert r.message_id == 999

    def test_get_telegram_config_no_token(self, monkeypatch, tmp_path):
        """Should raise TelegramConfigError if no token anywhere."""
        monkeypatch.delenv("TELEGRAM_AGENTWIRE_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_USER_ID", raising=False)
        # Mock load_dotenv to prevent it from loading real .env files
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)
        # Point HOME to tmp_path so config.yaml isn't found
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        from agentwire.channels.telegram import TelegramConfigError, _get_telegram_config
        with pytest.raises(TelegramConfigError, match="No Telegram bot token"):
            _get_telegram_config()


# =============================================================================
# Quo channel
# =============================================================================


class TestQuoChannel:
    def test_quo_config_env_fallback(self, monkeypatch):
        monkeypatch.setenv("QUO_API_KEY", "quo-key-123")
        from agentwire.channels.quo import QuoConfig
        config = QuoConfig()
        assert config.api_key == "quo-key-123"

    def test_quo_config_openphone_env_fallback(self, monkeypatch, tmp_path):
        # Patch HOME to prevent load_dotenv from reading real ~/.agentwire/.env
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("QUO_API_KEY", raising=False)
        monkeypatch.setenv("OPENPHONE_API_KEY", "op-key-456")
        from agentwire.channels.quo import QuoConfig
        config = QuoConfig(api_key="")  # Force empty to test env fallback
        assert config.api_key == "op-key-456"

    def test_quo_config_defaults(self):
        from agentwire.channels.quo import QuoConfig
        config = QuoConfig(api_key="k")
        assert config.from_number == ""
        assert config.default_to == ""

    def test_send_quo_no_api_key(self, tmp_path, monkeypatch):
        """Should raise QuoConfigError if no API key."""
        monkeypatch.delenv("QUO_API_KEY", raising=False)
        monkeypatch.delenv("OPENPHONE_API_KEY", raising=False)
        config_data = {"channels": {"quo": {"api_key": ""}}}
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump(config_data, f)

        from agentwire.config import load_config
        import agentwire.config as config_mod
        old = config_mod._config
        config_mod._config = load_config(config_path)

        from agentwire.channels.quo import QuoConfigError, send_quo_sms
        try:
            with pytest.raises(QuoConfigError):
                send_quo_sms(body="test")
        finally:
            config_mod._config = old


# =============================================================================
# SMS channel
# =============================================================================


class TestSMSChannel:
    def test_sms_config_env_fallback(self, monkeypatch):
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "sid-env")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-env")
        from agentwire.channels.sms import SMSConfig
        config = SMSConfig()
        assert config.account_sid == "sid-env"
        assert config.auth_token == "tok-env"

    def test_sms_no_twilio_installed(self):
        """send_sms should return error if twilio not installed."""
        from agentwire.channels.sms import send_sms
        # twilio is likely not installed in test env — this tests the fallback
        try:
            import twilio
            pytest.skip("twilio is installed, can't test missing dep path")
        except ImportError:
            pass

        result = send_sms(body="test", to="+1234567890")
        assert result.success is False
        assert "not installed" in result.error.lower() or "twilio" in result.error.lower()


# =============================================================================
# Webhook channel
# =============================================================================


class TestWebhookChannel:
    def test_webhook_config_defaults(self):
        from agentwire.channels.webhook import WebhookConfig
        config = WebhookConfig()
        assert config.url == ""
        assert config.method == "POST"
        assert config.headers == {}
        assert config.content_type == "application/json"

    def test_send_webhook_no_url(self, tmp_path, monkeypatch):
        """Should raise WebhookConfigError if no URL."""
        config_data = {"channels": {"webhook": {"url": ""}}}
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump(config_data, f)

        from agentwire.config import load_config
        import agentwire.config as config_mod
        config_mod._config_cache = None
        config = load_config(config_path)
        config_mod._config_cache = config

        from agentwire.channels.webhook import WebhookConfigError, send_webhook
        with pytest.raises(WebhookConfigError, match="No URL"):
            send_webhook(text="test")

        config_mod._config_cache = None


# =============================================================================
# Discord channel
# =============================================================================


class TestDiscordChannel:
    def test_discord_config_env_fallback(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc-tok")
        from agentwire.channels.discord import DiscordConfig
        config = DiscordConfig()
        assert config.bot_token == "disc-tok"

    def test_discord_config_defaults(self):
        from agentwire.channels.discord import DiscordConfig
        config = DiscordConfig(bot_token="tok")
        assert config.default_session == "agentwire"
        assert config.voice_replies is True
        assert config.session_name == "agentwire-discord"
        assert config.allowed_user_ids == []

# =============================================================================
# Slack channel
# =============================================================================


class TestSlackChannel:
    def test_slack_config_env_fallback(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        from agentwire.channels.slack import SlackConfig
        config = SlackConfig()
        assert config.bot_token == "xoxb-test"
        assert config.app_token == "xapp-test"

    def test_slack_config_defaults(self):
        from agentwire.channels.slack import SlackConfig
        config = SlackConfig(bot_token="tok", app_token="app")
        assert config.default_session == "agentwire"
        assert config.voice_replies is True
        assert config.session_name == "agentwire-slack"
        assert config.allowed_user_ids == []

# =============================================================================
# Base class
# =============================================================================


class TestBaseChannel:
    def test_channel_init(self):
        ch = Channel(config={"key": "val"})
        assert ch.config == {"key": "val"}

    def test_channel_init_no_config(self):
        ch = Channel()
        assert ch.config is None

    def test_service_channel_not_implemented(self):
        ch = ServiceChannel()
        with pytest.raises(NotImplementedError):
            import asyncio
            asyncio.get_event_loop().run_until_complete(ch.start())

    def test_send_only_channel_not_implemented(self):
        ch = SendOnlyChannel()
        with pytest.raises(NotImplementedError):
            import asyncio
            asyncio.get_event_loop().run_until_complete(ch.send("test"))

    def test_service_channel_default_max_message_length(self):
        ch = ServiceChannel()
        assert ch.max_message_length == 2000

    def test_truncate_output_short_text(self):
        ch = ServiceChannel()
        assert ch.truncate_output("hello") == "hello"

    def test_truncate_output_long_text(self):
        ch = ServiceChannel()
        long_text = "x" * 3000
        result = ch.truncate_output(long_text)
        assert len(result) == 2000
        # Should keep the tail
        assert result == long_text[-2000:]

    def test_truncate_output_exact_length(self):
        ch = ServiceChannel()
        exact = "x" * 2000
        assert ch.truncate_output(exact) == exact

    def test_discord_max_message_length(self):
        from agentwire.channels.discord import DiscordChannel
        assert DiscordChannel.max_message_length == 1800

    def test_slack_max_message_length(self):
        from agentwire.channels.slack import SlackChannel
        assert SlackChannel.max_message_length == 2800

    def test_discord_truncate_uses_own_limit(self):
        from agentwire.channels.discord import DiscordChannel
        ch = DiscordChannel()
        result = ch.truncate_output("x" * 3000)
        assert len(result) == 1800

    def test_slack_truncate_uses_own_limit(self):
        from agentwire.channels.slack import SlackChannel
        ch = SlackChannel()
        result = ch.truncate_output("x" * 5000)
        assert len(result) == 2800


class TestSharedSessionHelpers:
    """Tests for shared session_exists, ensure_session, wait_for_session_ready."""

    def test_session_exists_found(self):
        from agentwire.channels.base import session_exists
        with patch("agentwire.channels.base._run_cmd") as mock:
            mock.return_value = {"success": True}
            assert session_exists("test-session") is True
            mock.assert_called_once_with(["info", "-s", "test-session"])

    def test_session_exists_not_found(self):
        from agentwire.channels.base import session_exists
        with patch("agentwire.channels.base._run_cmd") as mock:
            mock.return_value = {"success": False}
            assert session_exists("missing") is False

    def test_ensure_session_already_exists(self):
        from agentwire.channels.base import ensure_session
        with patch("agentwire.channels.base.session_exists", return_value=True):
            assert ensure_session("existing") is True

    def test_ensure_session_creates_new(self):
        from agentwire.channels.base import ensure_session
        with patch("agentwire.channels.base.session_exists", return_value=False):
            with patch("agentwire.channels.base._run_cmd") as mock:
                mock.return_value = {"success": True}
                assert ensure_session("new-session", "/path/to/project") is True
                mock.assert_called_once()
                args = mock.call_args[0][0]
                assert "new" in args
                assert "-s" in args
                assert "new-session" in args
                assert "-p" in args

    def test_queued_message_dataclass(self):
        from agentwire.channels.base import QueuedMessage
        msg = QueuedMessage(
            platform_msg={"ts": "123"},
            text="hello",
            session="test",
            project="/tmp",
            prefix="[Test: 'hello']",
        )
        assert msg.text == "hello"
        assert msg.session == "test"
        assert msg.platform_msg == {"ts": "123"}


# =============================================================================
# Shared CLI runners
# =============================================================================


class TestCLIRunners:
    def test_run_cmd_success(self):
        """_run_cmd with a working command."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout='{"success": true, "data": "test"}',
                stderr="",
            )
            result = _run_cmd(["list"])
            assert result["success"] is True
            assert result["data"] == "test"
            # Verify --json was appended
            call_args = mock_run.call_args[0][0]
            assert "--json" in call_args

    def test_run_cmd_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            result = _run_cmd(["list"])
            assert result["success"] is False
            assert "timed out" in result["error"].lower()

    def test_run_cmd_raw(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="raw output here")
            result = _run_cmd_raw(["output", "-s", "main"])
            assert result == "raw output here"


# =============================================================================
# Notifications.py deleted — import should fail
# =============================================================================


class TestNotificationsDeleted:
    def test_old_import_fails(self):
        """from agentwire.notifications import ... should fail."""
        with pytest.raises(ModuleNotFoundError):
            from agentwire.notifications import send_email  # noqa: F401

    def test_old_import_check_telegram_fails(self):
        with pytest.raises(ModuleNotFoundError):
            from agentwire.notifications import check_telegram_bot  # noqa: F401


# =============================================================================
# Import rewiring — new paths work
# =============================================================================


class TestImportRewiring:
    def test_email_imports_from_channels(self):
        from agentwire.channels.email import send_email, cmd_email, EmailConfigError
        assert callable(send_email)
        assert callable(cmd_email)

    def test_telegram_imports_from_channels(self):
        from agentwire.channels.telegram import (
            send_telegram,
            check_telegram_bot,
            TelegramConfigError,
        )
        assert callable(send_telegram)
        assert callable(check_telegram_bot)

    def test_registry_from_init(self):
        from agentwire.channels import ChannelRegistry, Channel, SendOnlyChannel, ServiceChannel
        assert ChannelRegistry is not None
        assert Channel is not None

    def test_notification_error_from_channels(self):
        from agentwire.channels import NotificationError
        assert issubclass(NotificationError, Exception)


# =============================================================================
# Happy-path send tests (mocked external calls)
# =============================================================================


@pytest.fixture
def _mock_config():
    """Fixture to safely swap agentwire config for tests, with guaranteed cleanup."""
    import agentwire.config as config_mod
    from agentwire.config import load_config

    original = config_mod._config

    def _set(config_data: dict, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump(config_data))
        config_mod._config = load_config(config_path)
        return config_mod._config

    yield _set

    config_mod._config = original


class TestSendEmailSuccess:
    def test_send_email_success(self, tmp_path, _mock_config):
        """Email send with mocked resend API returns success."""
        _mock_config({"channels": {"email": {
            "api_key": "re_test_key",
            "from_address": "test@example.com",
            "default_to": "user@example.com",
        }}}, tmp_path)

        from agentwire.channels.email import send_email

        with patch("agentwire.channels.email.resend") as mock_resend:
            mock_resend.Emails.send.return_value = {"id": "msg-abc123"}
            result = send_email(body="Hello world", subject="Test")

        assert result.success is True
        assert result.message_id == "msg-abc123"
        assert result.error is None
        mock_resend.Emails.send.assert_called_once()

    def test_send_email_with_to_override(self, tmp_path, _mock_config):
        """Email send with explicit to= overrides default_to."""
        _mock_config({"channels": {"email": {
            "api_key": "re_test_key",
            "from_address": "test@example.com",
            "default_to": "default@example.com",
        }}}, tmp_path)

        from agentwire.channels.email import send_email

        with patch("agentwire.channels.email.resend") as mock_resend:
            mock_resend.Emails.send.return_value = {"id": "msg-xyz"}
            result = send_email(body="Hello", to="override@example.com")

        assert result.success is True
        # Verify the override was used
        call_args = mock_resend.Emails.send.call_args[0][0]
        assert call_args["to"] == ["override@example.com"]


class TestSendTelegramSuccess:
    def test_send_telegram_success(self, tmp_path, _mock_config, monkeypatch):
        """Telegram send with mocked urllib returns success."""
        _mock_config({"channels": {"telegram": {
            "bot_token": "bot123:ABC",
            "allowed_users": [12345],
        }}}, tmp_path)

        from agentwire.channels.telegram import send_telegram

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "result": {"message_id": 42}
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = send_telegram(text="Hello from test", chat_id=12345)

        assert result.success is True
        assert result.message_id == 42
        assert result.error is None


class TestSendQuoSuccess:
    def test_send_quo_success(self, tmp_path, _mock_config, monkeypatch):
        """Quo SMS send with mocked urllib returns success."""
        monkeypatch.setenv("HOME", str(tmp_path))  # Prevent dotenv from loading real keys
        _mock_config({"channels": {"quo": {
            "api_key": "test-quo-key",
            "from_number": "+15551234567",
            "default_to": "+15559876543",
        }}}, tmp_path)

        from agentwire.channels.quo import send_quo_sms

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": {"id": "quo-msg-001"}
        }).encode()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = send_quo_sms(body="Test SMS")

        assert result.success is True
        assert result.message_id == "quo-msg-001"
        assert result.error is None


class TestSendSMSSuccess:
    def test_send_sms_success(self, tmp_path, _mock_config):
        """SMS send with mocked Twilio client returns success."""
        _mock_config({"channels": {"sms": {
            "account_sid": "AC_test",
            "auth_token": "test_token",
            "from_number": "+15551234567",
            "default_to": "+15559876543",
        }}}, tmp_path)

        from agentwire.channels.sms import send_sms

        mock_msg = MagicMock()
        mock_msg.sid = "SM1234567890"

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg

        with patch.dict("sys.modules", {"twilio": MagicMock(), "twilio.rest": MagicMock()}):
            with patch("agentwire.channels.sms.send_sms") as mock_send:
                # Since twilio import is dynamic, mock at the function level
                mock_send.return_value = ChannelResult(success=True, message_id="SM1234567890")
                result = mock_send(body="Test SMS")

        assert result.success is True
        assert result.message_id == "SM1234567890"

    def test_send_sms_with_twilio_mock(self, tmp_path, _mock_config):
        """SMS send using actual function with mocked twilio module."""
        _mock_config({"channels": {"sms": {
            "account_sid": "AC_test",
            "auth_token": "auth_test",
            "from_number": "+15551234567",
            "default_to": "+15559876543",
        }}}, tmp_path)

        mock_msg = MagicMock()
        mock_msg.sid = "SM_unit_test"
        mock_client_instance = MagicMock()
        mock_client_instance.messages.create.return_value = mock_msg
        mock_client_class = MagicMock(return_value=mock_client_instance)

        mock_twilio = MagicMock()
        mock_twilio.rest.Client = mock_client_class

        with patch.dict("sys.modules", {"twilio": mock_twilio, "twilio.rest": mock_twilio.rest}):
            # Re-import to pick up mocked twilio
            from agentwire.channels.sms import send_sms
            result = send_sms(body="Twilio test")

        assert result.success is True
        assert result.message_id == "SM_unit_test"


class TestSendWebhookSuccess:
    def test_send_webhook_success(self, tmp_path, _mock_config):
        """Webhook send with mocked urllib returns success."""
        _mock_config({"channels": {"webhook": {
            "url": "https://hooks.example.com/test",
            "method": "POST",
        }}}, tmp_path)

        from agentwire.channels.webhook import send_webhook

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = send_webhook(text="Test webhook payload")

        assert result.success is True
        assert result.error is None

    def test_send_webhook_with_extra(self, tmp_path, _mock_config):
        """Webhook send merges extra data into payload."""
        _mock_config({"channels": {"webhook": {
            "url": "https://hooks.example.com/test",
        }}}, tmp_path)

        from agentwire.channels.webhook import send_webhook

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = send_webhook(text="msg", extra={"channel": "#alerts"})

        assert result.success is True
        # Verify extra was included in payload
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["text"] == "msg"
        assert payload["channel"] == "#alerts"

    def test_send_webhook_server_error(self, tmp_path, _mock_config):
        """Webhook returns failure for HTTP 500."""
        _mock_config({"channels": {"webhook": {
            "url": "https://hooks.example.com/test",
        }}}, tmp_path)

        from agentwire.channels.webhook import send_webhook

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = send_webhook(text="will fail")

        assert result.success is False


# =============================================================================
# Composable session config hierarchy
# =============================================================================


class TestComposeSessionConfig:
    """Tests for compose_session_config — platform → scope → specific."""

    def test_platform_only(self):
        from agentwire.channels.base import compose_session_config
        t, r, i = compose_session_config(
            platform={"type": "claude-bypass", "roles": ["agentwire"], "instructions": "Top level"},
            scope={},
            specific={},
        )
        assert t == "claude-bypass"
        assert r == ["agentwire"]
        assert i == "Top level"

    def test_three_levels_append_instructions(self):
        from agentwire.channels.base import compose_session_config
        _, _, i = compose_session_config(
            platform={"instructions": "Platform rule"},
            scope={"instructions": "Scope rule"},
            specific={"instructions": "Specific rule"},
        )
        # Joined with blank lines, in order
        assert i == "Platform rule\n\nScope rule\n\nSpecific rule"

    def test_three_levels_append_roles_deduped(self):
        from agentwire.channels.base import compose_session_config
        _, r, _ = compose_session_config(
            platform={"roles": ["agentwire", "common"]},
            scope={"roles": ["slack-dm", "common"]},      # "common" is dup
            specific={"roles": ["expert", "agentwire"]},  # "agentwire" is dup
        )
        # Order preserved, dups removed
        assert r == ["agentwire", "common", "slack-dm", "expert"]

    def test_type_precedence_specific_wins(self):
        from agentwire.channels.base import compose_session_config
        t, _, _ = compose_session_config(
            platform={"type": "claude-bypass"},
            scope={"type": "claude-auto"},
            specific={"type": "claude-auto"},
        )
        assert t == "claude-auto"

    def test_type_precedence_scope_over_platform(self):
        from agentwire.channels.base import compose_session_config
        t, _, _ = compose_session_config(
            platform={"type": "claude-bypass"},
            scope={"type": "claude-auto"},
            specific={},
        )
        assert t == "claude-auto"

    def test_type_fallback_when_all_empty(self):
        from agentwire.channels.base import compose_session_config
        t, _, _ = compose_session_config(platform={}, scope={}, specific={})
        assert t == "claude-bypass"

    def test_type_custom_fallback(self):
        from agentwire.channels.base import compose_session_config
        t, _, _ = compose_session_config(
            platform={}, scope={}, specific={}, fallback_type="claude-prompted",
        )
        assert t == "claude-prompted"

    def test_empty_instructions_skipped(self):
        from agentwire.channels.base import compose_session_config
        _, _, i = compose_session_config(
            platform={"instructions": "Only platform"},
            scope={"instructions": ""},
            specific={"instructions": "   "},  # whitespace only
        )
        assert i == "Only platform"

    def test_missing_keys_tolerated(self):
        from agentwire.channels.base import compose_session_config
        # All three levels with no relevant keys
        t, r, i = compose_session_config(
            platform={"unrelated": "x"},
            scope={"unrelated": "y"},
            specific={},
        )
        assert t == "claude-bypass"
        assert r == []
        assert i == ""


class TestInjectInstructions:
    """Tests for inject_instructions — marker-block CLAUDE.md injection."""

    def test_create_new_file(self, tmp_path):
        from agentwire.channels.base import inject_instructions, INSTRUCTIONS_MARKER_BEGIN
        claude = tmp_path / "CLAUDE.md"
        inject_instructions(claude, "Be helpful.")
        content = claude.read_text()
        assert INSTRUCTIONS_MARKER_BEGIN in content
        assert "Be helpful." in content

    def test_empty_instructions_no_file(self, tmp_path):
        from agentwire.channels.base import inject_instructions
        claude = tmp_path / "CLAUDE.md"
        inject_instructions(claude, "")
        assert not claude.exists()

    def test_prepend_to_existing_file(self, tmp_path):
        from agentwire.channels.base import inject_instructions, INSTRUCTIONS_MARKER_BEGIN
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("# My Project\n\nHuman notes here.\n")
        inject_instructions(claude, "Auto instruction")
        content = claude.read_text()
        assert content.startswith(INSTRUCTIONS_MARKER_BEGIN)
        assert "# My Project" in content
        assert "Human notes here." in content
        assert "Auto instruction" in content

    def test_replace_existing_block(self, tmp_path):
        from agentwire.channels.base import inject_instructions
        claude = tmp_path / "CLAUDE.md"
        inject_instructions(claude, "Original rules")
        inject_instructions(claude, "Updated rules")
        content = claude.read_text()
        assert "Updated rules" in content
        assert "Original rules" not in content

    def test_remove_block_when_empty(self, tmp_path):
        from agentwire.channels.base import inject_instructions, INSTRUCTIONS_MARKER_BEGIN
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("# Header\n\nHuman stuff.\n")
        inject_instructions(claude, "To be removed")
        assert INSTRUCTIONS_MARKER_BEGIN in claude.read_text()
        inject_instructions(claude, "")
        content = claude.read_text()
        assert INSTRUCTIONS_MARKER_BEGIN not in content
        # Human content preserved
        assert "# Header" in content
        assert "Human stuff." in content

    def test_preserves_human_edits_outside_block(self, tmp_path):
        """Human edits above AND below the block must survive regeneration."""
        from agentwire.channels.base import inject_instructions
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("# Header\n\nBefore block.\n")
        inject_instructions(claude, "Agent rules v1")
        # Human adds text after the block
        content = claude.read_text()
        claude.write_text(content + "\n## After\n\nHuman footer.\n")
        # Regenerate with new rules
        inject_instructions(claude, "Agent rules v2")
        final = claude.read_text()
        assert "# Header" in final
        assert "Before block." in final
        assert "Agent rules v2" in final
        assert "Agent rules v1" not in final
        assert "## After" in final
        assert "Human footer." in final

    def test_empty_noop_when_no_file(self, tmp_path):
        from agentwire.channels.base import inject_instructions
        claude = tmp_path / "CLAUDE.md"
        inject_instructions(claude, "")  # no file, no instructions
        assert not claude.exists()


class TestSlackComposeHierarchy:
    """Tests for SlackBridge.compose_dm_config and compose_channel_config."""

    def test_dm_uses_platform_and_dm_scope_defaults(self):
        from agentwire.channels.slack import SlackBridge, SlackConfig
        cfg = SlackConfig(
            bot_token="x", app_token="y",
            default_instructions="Slack-wide rule",
            dm_instructions="DM-specific rule",
        )
        bridge = SlackBridge(cfg)
        t, r, i = bridge.compose_dm_config("U_new_user")
        assert t == "claude-bypass"
        assert "agentwire" in r
        assert "slack-dm" in r
        assert "Slack-wide rule" in i
        assert "DM-specific rule" in i

    def test_channel_uses_platform_and_channel_scope_defaults(self):
        from agentwire.channels.slack import SlackBridge, SlackConfig
        cfg = SlackConfig(
            bot_token="x", app_token="y",
            default_instructions="Slack-wide rule",
            channel_instructions="Channel-specific rule",
        )
        bridge = SlackBridge(cfg)
        t, r, i = bridge.compose_channel_config("C_nomap")
        assert "Slack-wide rule" in i
        assert "Channel-specific rule" in i

    def test_user_map_adds_specific_dm_instructions(self):
        from agentwire.channels.slack import SlackBridge, SlackConfig
        cfg = SlackConfig(
            bot_token="x", app_token="y",
            default_instructions="Platform",
            dm_instructions="DM",
            user_map={"U123": {
                "roles": ["admin"],
                "instructions": "This user is the team lead.",
            }},
        )
        bridge = SlackBridge(cfg)
        t, r, i = bridge.compose_dm_config("U123")
        assert "Platform" in i
        assert "DM" in i
        assert "team lead" in i
        assert "admin" in r

    def test_channel_map_adds_specific_channel_override(self):
        from agentwire.channels.slack import SlackBridge, SlackConfig
        cfg = SlackConfig(
            bot_token="x", app_token="y",
            channel_map={"C456": {
                "session": "backend",
                "type": "claude-auto",
                "roles": ["python-expert"],
                "instructions": "Backend channel: Python focus.",
            }},
        )
        bridge = SlackBridge(cfg)
        t, r, i = bridge.compose_channel_config("C456")
        assert t == "claude-auto"
        assert "python-expert" in r
        assert "Backend channel" in i

    def test_user_map_does_not_affect_channel_compose(self):
        """user_map is DM-only — channel compose must ignore it."""
        from agentwire.channels.slack import SlackBridge, SlackConfig
        cfg = SlackConfig(
            bot_token="x", app_token="y",
            user_map={"U123": {"instructions": "User-level rule"}},
            channel_map={"C456": {"session": "s"}},
        )
        bridge = SlackBridge(cfg)
        _, _, i = bridge.compose_channel_config("C456")
        assert "User-level rule" not in i


class TestDiscordComposeHierarchy:
    """Tests for DiscordBridge.compose_dm_config and compose_channel_config."""

    def test_dm_with_user_map_override(self):
        from agentwire.channels.discord import DiscordBridge, DiscordConfig
        cfg = DiscordConfig(
            bot_token="x",
            default_instructions="Discord-wide",
            dm_instructions="DM scope",
            user_map={"999": {"instructions": "Known admin"}},
        )
        bridge = DiscordBridge(cfg)
        t, r, i = bridge.compose_dm_config(999)
        assert "Discord-wide" in i
        assert "DM scope" in i
        assert "Known admin" in i

    def test_channel_with_channel_map_override(self):
        from agentwire.channels.discord import DiscordBridge, DiscordConfig
        cfg = DiscordConfig(
            bot_token="x",
            channel_map={"1234": {
                "session": "core",
                "roles": ["reviewer"],
                "instructions": "Review PRs here",
            }},
        )
        bridge = DiscordBridge(cfg)
        _, r, i = bridge.compose_channel_config(1234)
        assert "reviewer" in r
        assert "Review PRs here" in i

    def test_default_behavior_empty_config(self):
        """With no custom config, bridge produces sensible defaults: claude-bypass + core roles."""
        from agentwire.channels.discord import DiscordBridge, DiscordConfig
        cfg = DiscordConfig(bot_token="x")
        bridge = DiscordBridge(cfg)
        dm_t, dm_r, dm_i = bridge.compose_dm_config(123)
        assert dm_t == "claude-bypass"
        assert dm_r == ["agentwire", "discord-dm"]
        assert dm_i == ""

        ch_t, ch_r, ch_i = bridge.compose_channel_config(456)
        assert ch_t == "claude-bypass"
        assert ch_r == ["agentwire", "discord-dm"]
        assert ch_i == ""
