"""Tests for agentwire/channels/ — registry, base classes, email, quo."""

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agentwire.channels.base import (
    Channel,
    ChannelRegistry,
    ChannelResult,
    NotificationError,
    SendOnlyChannel,
)


class TestChannelRegistry:
    def test_builtin_channels_registered(self):
        """email + quo + push (Web Push, #483) are the built-in send-only channels."""
        channels = ChannelRegistry.all()
        assert set(channels.keys()) == {"email", "quo", "push"}

    def test_get_existing(self):
        cls = ChannelRegistry.get("email")
        assert cls is not None
        assert cls.name == "email"

    def test_get_nonexistent(self):
        assert ChannelRegistry.get("nonexistent_channel") is None

    def test_register_decorator(self):
        @ChannelRegistry.register("_test_channel")
        class _TestChannel(SendOnlyChannel):
            name = "_test_channel"

        assert "_test_channel" in ChannelRegistry._channels
        del ChannelRegistry._channels["_test_channel"]

    @pytest.mark.parametrize(
        "module,cls_name,expected_name,expected_type,expected_config_key",
        [
            ("agentwire.channels.email", "EmailChannel", "email", "send_only", "email"),
            ("agentwire.channels.quo", "QuoChannel", "quo", "send_only", "quo"),
        ],
    )
    def test_channel_class_metadata(
        self, module, cls_name, expected_name, expected_type, expected_config_key
    ):
        import importlib
        cls = getattr(importlib.import_module(module), cls_name)
        assert cls.name == expected_name
        assert cls.channel_type == expected_type
        assert cls.config_key == expected_config_key


class TestResolveConfig:
    def test_resolve_existing(self):
        data = {"channels": {"email": {"from_address": "x@y.com"}}}
        cfg = ChannelRegistry.resolve_config("email", data)
        assert cfg == {"from_address": "x@y.com"}

    def test_resolve_unregistered_channel(self):
        assert ChannelRegistry.resolve_config("nope", {"channels": {"nope": {}}}) == {}

    def test_resolve_missing_section(self):
        assert ChannelRegistry.resolve_config("email", {}) == {}


class TestBaseChannel:
    def test_channel_defaults(self):
        ch = Channel()
        assert ch.config is None
        assert ch.name == ""

    def test_sendonly_send_not_implemented(self):
        ch = SendOnlyChannel()
        import asyncio
        with pytest.raises(NotImplementedError):
            asyncio.run(ch.send("hi"))

    def test_channel_result_defaults(self):
        r = ChannelResult(success=True)
        assert r.success is True
        assert r.message_id is None
        assert r.error is None

    def test_notification_error_is_exception(self):
        assert issubclass(NotificationError, Exception)


class TestEmailChannel:
    def test_email_config_key_from_env(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "env-key-123")
        from agentwire.channels.email import EmailConfig
        config = EmailConfig()
        assert config.api_key == "env-key-123"

    def test_email_config_rejects_explicit_key(self):
        """Keys are env-only (~/.agentwire/.env) — not a config field."""
        from agentwire.channels.email import EmailConfig
        with pytest.raises(TypeError):
            EmailConfig(api_key="explicit-key")

    def test_config_yaml_api_key_ignored(self, tmp_path, monkeypatch, capsys):
        """A stale api_key in config.yaml warns and is ignored — env wins."""
        monkeypatch.setenv("RESEND_API_KEY", "env-key")
        config_data = {"channels": {"email": {
            "api_key": "stale-yaml-key",
            "from_address": "x@y.com",
        }}}
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump(config_data))

        from agentwire.config import load_config
        config = load_config(config_path)
        assert config.channels["email"].api_key == "env-key"
        assert config.channels["email"].from_address == "x@y.com"
        assert "api_key" in capsys.readouterr().err

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
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        config_data = {"channels": {"email": {"from_address": "x@y.com"}}}
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump(config_data))

        import agentwire.config as config_mod
        from agentwire.config import load_config
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
            (["a@x.com,b@x.com", "c@x.com"], "", ["a@x.com", "b@x.com", "c@x.com"]),
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


class TestQuoChannel:
    def test_quo_config_key_from_env(self, monkeypatch):
        monkeypatch.setenv("QUO_API_KEY", "quo-key-123")
        from agentwire.channels.quo import QuoConfig
        config = QuoConfig()
        assert config.api_key == "quo-key-123"

    def test_quo_config_openphone_env_fallback(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("QUO_API_KEY", raising=False)
        monkeypatch.setenv("OPENPHONE_API_KEY", "op-key-456")
        from agentwire.channels.quo import QuoConfig
        config = QuoConfig()
        assert config.api_key == "op-key-456"

    def test_quo_config_rejects_explicit_key(self):
        """Keys are env-only (~/.agentwire/.env) — not a config field."""
        from agentwire.channels.quo import QuoConfig
        with pytest.raises(TypeError):
            QuoConfig(api_key="k")

    def test_quo_config_defaults(self):
        from agentwire.channels.quo import QuoConfig
        config = QuoConfig()
        assert config.from_number == ""
        assert config.default_to == ""

    def test_send_quo_no_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("QUO_API_KEY", raising=False)
        monkeypatch.delenv("OPENPHONE_API_KEY", raising=False)
        config_data = {"channels": {"quo": {}}}
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump(config_data))

        import agentwire.config as config_mod
        from agentwire.config import load_config
        old = config_mod._config
        config_mod._config = load_config(config_path)

        from agentwire.channels.quo import QuoConfigError, send_quo_sms
        try:
            with pytest.raises(QuoConfigError):
                send_quo_sms(body="test")
        finally:
            config_mod._config = old


@pytest.fixture
def _mock_config():
    """Swap agentwire config for tests, restore on teardown."""
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
    def test_send_email_success(self, tmp_path, _mock_config, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
        _mock_config({"channels": {"email": {
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

    def test_send_email_with_to_override(self, tmp_path, _mock_config, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
        _mock_config({"channels": {"email": {
            "from_address": "test@example.com",
            "default_to": "default@example.com",
        }}}, tmp_path)

        from agentwire.channels.email import send_email

        with patch("agentwire.channels.email.resend") as mock_resend:
            mock_resend.Emails.send.return_value = {"id": "msg-xyz"}
            result = send_email(body="Hello", to="override@example.com")

        assert result.success is True
        call_args = mock_resend.Emails.send.call_args[0][0]
        assert call_args["to"] == ["override@example.com"]


class TestSendQuoSuccess:
    def test_send_quo_success(self, tmp_path, _mock_config, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("QUO_API_KEY", "test-quo-key")
        _mock_config({"channels": {"quo": {
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
