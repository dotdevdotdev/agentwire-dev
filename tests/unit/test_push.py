"""Unit tests for Web Push (#483) — subscription store + VAPID sender plumbing."""

import base64

import pytest

from agentwire import push_store
from agentwire.channels import push as push_channel


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "push_subscriptions.json"


def _sub(endpoint="https://push.example/abc", auth="a", p256dh="p"):
    return dict(endpoint=endpoint, keys={"auth": auth, "p256dh": p256dh})


# ---------------------------------------------------------------------------
# Subscription store
# ---------------------------------------------------------------------------


class TestPushStore:
    def test_add_and_load(self, store_path):
        push_store.add(**_sub(), device="phone", path=store_path)
        subs = push_store.load(store_path)
        assert len(subs) == 1
        assert subs[0].endpoint == "https://push.example/abc"
        assert subs[0].device == "phone"
        assert subs[0].keys == {"auth": "a", "p256dh": "p"}

    def test_add_is_idempotent_by_endpoint(self, store_path):
        push_store.add(**_sub(), path=store_path)
        push_store.add(**_sub(auth="updated"), path=store_path)
        subs = push_store.load(store_path)
        assert len(subs) == 1  # upsert, not duplicate
        assert subs[0].keys["auth"] == "updated"

    def test_distinct_endpoints_coexist(self, store_path):
        push_store.add(**_sub(endpoint="https://a"), path=store_path)
        push_store.add(**_sub(endpoint="https://b"), path=store_path)
        assert {s.endpoint for s in push_store.load(store_path)} == {"https://a", "https://b"}

    def test_remove(self, store_path):
        push_store.add(**_sub(), path=store_path)
        assert push_store.remove("https://push.example/abc", path=store_path) is True
        assert push_store.load(store_path) == []
        assert push_store.remove("https://push.example/abc", path=store_path) is False

    def test_prune(self, store_path):
        push_store.add(**_sub(endpoint="https://a"), path=store_path)
        push_store.add(**_sub(endpoint="https://b"), path=store_path)
        removed = push_store.prune(["https://a", "https://missing"], path=store_path)
        assert removed == 1
        assert {s.endpoint for s in push_store.load(store_path)} == {"https://b"}

    def test_to_webpush_shape(self, store_path):
        push_store.add(**_sub(), path=store_path)
        sub = push_store.load(store_path)[0]
        assert sub.to_webpush() == {
            "endpoint": "https://push.example/abc",
            "keys": {"auth": "a", "p256dh": "p"},
        }


# ---------------------------------------------------------------------------
# VAPID key generation
# ---------------------------------------------------------------------------


class TestVapidKeygen:
    def test_keys_are_base64url_and_correct_length(self):
        priv, pub = push_channel.generate_vapid_keys()
        # Public key is an uncompressed EC point: 0x04 || X(32) || Y(32) = 65 bytes.
        pub_bytes = base64.urlsafe_b64decode(pub + "=" * (-len(pub) % 4))
        assert len(pub_bytes) == 65
        assert pub_bytes[0] == 0x04
        # Private key is the raw 32-byte scalar.
        priv_bytes = base64.urlsafe_b64decode(priv + "=" * (-len(priv) % 4))
        assert len(priv_bytes) == 32

    def test_keys_are_unique(self):
        assert push_channel.generate_vapid_keys()[0] != push_channel.generate_vapid_keys()[0]


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

    def test_send_noop_when_keys_missing(self, monkeypatch):
        cfg = push_channel.PushConfig()
        cfg.enabled = True
        cfg.vapid_private_key = ""
        cfg.vapid_public_key = ""
        monkeypatch.setattr(push_channel, "_get_push_config", lambda: cfg)
        ready, reason = push_channel.push_ready()
        assert ready is False
        assert "VAPID" in reason

    def test_send_reports_no_subscriptions(self, monkeypatch, store_path):
        cfg = push_channel.PushConfig()
        cfg.enabled = True
        cfg.vapid_private_key, cfg.vapid_public_key = push_channel.generate_vapid_keys()
        monkeypatch.setattr(push_channel, "_get_push_config", lambda: cfg)
        monkeypatch.setattr(push_store, "SUBSCRIPTIONS_FILE", store_path)
        result = push_channel.send_web_push("hi", "body")
        assert result.success is True
        assert result.sent == 0
