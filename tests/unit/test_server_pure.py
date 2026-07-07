"""Tests for pure functions and dataclasses in agentwire/server.py."""

import asyncio

from agentwire.server import PendingPermission, SessionConfig

# ---------------------------------------------------------------------------
# SessionConfig
# ---------------------------------------------------------------------------


class TestSessionConfig:
    def test_defaults(self):
        cfg = SessionConfig()
        assert cfg.voice == "default"
        assert cfg.posture == "bypass"
        assert cfg.roles == []

    def test_roles_none_to_empty_list(self):
        cfg = SessionConfig(roles=None)
        assert cfg.roles == []

    def test_custom_values(self):
        cfg = SessionConfig(
            voice="alice",
            posture="bare",
            roles=["voice", "worker"],
            machine="gpu-box",
        )
        assert cfg.voice == "alice"
        assert cfg.posture == "bare"
        assert cfg.roles == ["voice", "worker"]
        assert cfg.machine == "gpu-box"


# ---------------------------------------------------------------------------
# PendingPermission
# ---------------------------------------------------------------------------


class TestPendingPermission:
    def test_defaults(self):
        pp = PendingPermission(request={"tool": "Bash"})
        assert isinstance(pp.event, asyncio.Event)
        assert pp.decision is None
        assert pp.request == {"tool": "Bash"}

    def test_event_not_set(self):
        pp = PendingPermission(request={})
        assert not pp.event.is_set()
