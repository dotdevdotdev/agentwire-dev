"""Unit tests for agentwire.devices — registry, hashing, pairing lifecycle."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentwire import devices
from agentwire.devices import (
    DeviceRegistry,
    consume_pairing,
    create_pairing,
    hash_token,
    load_registry_cached,
)


@pytest.fixture
def reg_path(tmp_path):
    return tmp_path / "devices.json"


# ---------------------------------------------------------------------------
# Hashing / token issuance
# ---------------------------------------------------------------------------


class TestHashing:
    def test_hash_is_stable_and_not_plaintext(self):
        tok = "super-secret"
        assert hash_token(tok) == hash_token(tok)
        assert tok not in hash_token(tok)
        assert len(hash_token(tok)) == 64  # sha256 hex

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
    def test_add_and_resolve(self, reg_path):
        reg = DeviceRegistry(reg_path)
        device, token = reg.add("phone")
        resolved = DeviceRegistry.load(reg_path).resolve(token)
        assert resolved is not None
        assert resolved.id == device.id

    def test_resolve_unknown_token_is_none(self, reg_path):
        reg = DeviceRegistry(reg_path)
        reg.add("phone")
        assert DeviceRegistry.load(reg_path).resolve("nope") is None

    def test_revoke_blocks_resolution(self, reg_path):
        reg = DeviceRegistry(reg_path)
        device, token = reg.add("phone")
        assert reg.revoke(device.id) is True
        assert DeviceRegistry.load(reg_path).resolve(token) is None

    def test_revoke_one_keeps_others(self, reg_path):
        reg = DeviceRegistry(reg_path)
        d1, t1 = reg.add("laptop")
        d2, t2 = reg.add("phone")
        reg.revoke(d1.id)
        fresh = DeviceRegistry.load(reg_path)
        assert fresh.resolve(t1) is None
        assert fresh.resolve(t2) is not None

    def test_revoke_unknown_returns_false(self, reg_path):
        reg = DeviceRegistry(reg_path)
        assert reg.revoke("dev_nope") is False

    def test_public_omits_hash(self, reg_path):
        reg = DeviceRegistry(reg_path)
        device, _ = reg.add("phone")
        assert "token_hash" not in device.public()
        assert device.public()["id"] == device.id

    def test_file_is_owner_only(self, reg_path):
        reg = DeviceRegistry(reg_path)
        reg.add("phone")
        assert (reg_path.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# mtime cache
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_refreshes_after_revoke(self, reg_path, monkeypatch):
        monkeypatch.setattr(devices, "DEVICES_FILE", reg_path)
        devices._cache.clear()
        reg = DeviceRegistry(reg_path)
        device, token = reg.add("phone")
        # Prime the cache, then revoke through a separate handle.
        assert load_registry_cached(reg_path).resolve(token) is not None
        DeviceRegistry.load(reg_path).revoke(device.id)
        # mtime changed → cache must reparse and see the revocation.
        assert load_registry_cached(reg_path).resolve(token) is None


# ---------------------------------------------------------------------------
# Pairing lifecycle
# ---------------------------------------------------------------------------


class TestPairing:
    def test_create_and_consume(self, tmp_path):
        path = tmp_path / "pairings.json"
        pairing = create_pairing("phone", path=path)
        consumed = consume_pairing(pairing.code, path=path)
        assert consumed is not None
        assert consumed.name == "phone"

    def test_consume_is_one_shot(self, tmp_path):
        path = tmp_path / "pairings.json"
        pairing = create_pairing("phone", path=path)
        assert consume_pairing(pairing.code, path=path) is not None
        assert consume_pairing(pairing.code, path=path) is None

    def test_consume_unknown_code(self, tmp_path):
        path = tmp_path / "pairings.json"
        create_pairing("phone", path=path)
        assert consume_pairing("BOGUS123", path=path) is None

    def test_expired_code_rejected(self, tmp_path):
        path = tmp_path / "pairings.json"
        pairing = create_pairing("phone", ttl=-1, path=path)
        assert pairing.expired() is True
        assert consume_pairing(pairing.code, path=path) is None

    def test_consume_case_insensitive(self, tmp_path):
        path = tmp_path / "pairings.json"
        pairing = create_pairing("phone", path=path)
        assert consume_pairing(pairing.code.lower(), path=path) is not None


# ---------------------------------------------------------------------------
# Concurrency — the race window where B1/B2 hid (PR #458 red-team)
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_revoke_survives_concurrent_touches(self, reg_path):
        """B1: a touch racing a revoke must NOT resurrect the device.

        Each worker loads a snapshot (active), then touches — exactly the
        unlocked read-modify-write that used to wipe revoked=true. With the
        re-read-under-lock fix, the device must end revoked regardless of order.
        """
        reg = DeviceRegistry(reg_path)
        device, token = reg.add("phone")
        # Make last_seen stale so touch() actually wants to write.
        from agentwire.devices import _read_devices, _atomic_write_json, _iso
        from dataclasses import asdict
        ds = _read_devices(reg_path)
        ds[0].last_seen = _iso(0)
        _atomic_write_json(reg_path, {"devices": [asdict(d) for d in ds]})

        def toucher():
            r = DeviceRegistry.load(reg_path)  # snapshot: active
            time.sleep(0.001)
            r.touch(device.id)

        def revoker():
            time.sleep(0.001)
            DeviceRegistry.load(reg_path).revoke(device.id)

        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(toucher) for _ in range(11)] + [ex.submit(revoker)]
            for f in futs:
                f.result()

        final = DeviceRegistry.load(reg_path)
        assert final.devices[0].revoked is True
        assert final.resolve(token) is None

    def test_concurrent_redeem_is_single_use(self, tmp_path):
        """B2: many concurrent redemptions of one code → exactly one winner."""
        path = tmp_path / "pairings.json"
        pairing = create_pairing("phone", path=path)

        def redeem():
            return consume_pairing(pairing.code, path=path)

        with ThreadPoolExecutor(max_workers=16) as ex:
            results = [f.result() for f in [ex.submit(redeem) for _ in range(16)]]

        winners = [r for r in results if r is not None]
        assert len(winners) == 1

    def test_concurrent_adds_all_persist(self, reg_path):
        """Parallel pairings must not clobber each other (lost-update guard)."""
        def add(i):
            return DeviceRegistry.load(reg_path).add(f"dev{i}")

        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(add, range(20)))

        assert len(DeviceRegistry.load(reg_path).devices) == 20
