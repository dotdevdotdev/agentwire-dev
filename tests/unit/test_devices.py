"""Unit tests for agentwire.devices — registry, hashing, pairing lifecycle."""

import pytest

from agentwire.devices import (
    DeviceRegistry,
    consume_pairing,
    create_pairing,
    hash_token,
)


@pytest.fixture
def reg_path(tmp_path):
    return tmp_path / "devices.json"


# ---------------------------------------------------------------------------
# Hashing / token issuance
# ---------------------------------------------------------------------------


class TestHashing:
    def test_added_device_stores_hash_not_token(self, reg_path):
        reg = DeviceRegistry(reg_path)
        device, token = reg.add("laptop")
        assert device.token_hash == hash_token(token)
        # The plaintext token never appears in the persisted file.
        assert token not in reg_path.read_text()


# ---------------------------------------------------------------------------
# Registry add / resolve / revoke
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_revoke_blocks_resolution(self, reg_path):
        reg = DeviceRegistry(reg_path)
        device, token = reg.add("phone")
        assert reg.revoke(device.id) is True
        assert DeviceRegistry.load(reg_path).resolve(token) is None


# ---------------------------------------------------------------------------
# mtime cache
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pairing lifecycle
# ---------------------------------------------------------------------------


class TestPairing:
    def test_consume_is_one_shot(self, tmp_path):
        path = tmp_path / "pairings.json"
        pairing = create_pairing("phone", path=path)
        assert consume_pairing(pairing.code, path=path) is not None
        assert consume_pairing(pairing.code, path=path) is None


# ---------------------------------------------------------------------------
# Concurrency — the race window where B1/B2 hid (PR #458 red-team)
# ---------------------------------------------------------------------------
