"""Tests for agentwire/channels/ — registry, base classes, email, quo."""

from unittest.mock import patch

import pytest
import yaml


class TestEmailChannel:


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



