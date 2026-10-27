"""Unit tests for Web Push (#483) — subscription store + VAPID sender plumbing."""

from agentwire.channels import push as push_channel

# ---------------------------------------------------------------------------
# Subscription store
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# VAPID key generation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sender gating — disabled / unconfigured is a clean no-op, never a raise
# ---------------------------------------------------------------------------


class TestSendGating:
    def test_send_noop_when_disabled(self, monkeypatch):
        cfg = push_channel.PushConfig()  # enabled defaults False
        monkeypatch.setattr(push_channel, "_get_push_config", lambda: cfg)
        result = push_channel.send_web_push("hi", "body")
        assert result.success is False
        assert "enabled" in result.error
