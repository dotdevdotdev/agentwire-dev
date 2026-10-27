"""Tests for agentwire/services.py — registry, healthchecks, watchdog policy (#214)."""

import pytest

from agentwire import services
from agentwire.config import (
    Config,
    CustomServiceConfig,
    ServicesConfig,
)
from agentwire.services import WatchdogState


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "services-state.json"
    monkeypatch.setattr(services, "STATE_FILE", f)
    return f


class TestWatchdogState:
    """The pure restart/notify policy — the heart of the watchdog."""

    def test_transition_to_down_notifies_and_restarts(self):
        s = WatchdogState()
        s.on_check(1000, True, "on-failure")
        actions = s.on_check(1060, False, "on-failure")
        assert actions == ["notify_down", "restart"]


class TestServicesCLI:
    """cmd_services_* — JSON contracts the portal watchdog depends on."""

    @pytest.fixture
    def cli(self, state_file, monkeypatch):
        from agentwire import system_cli as main_mod

        monkeypatch.setattr(services, "notifications_session_name", lambda: "notif")
        monkeypatch.setattr(services, "_source_dir", lambda: "/tmp/src")
        cfg = Config(services=ServicesConfig(custom=[CustomServiceConfig(name="tracker")]))
        monkeypatch.setattr("agentwire.config.load_config", lambda *a, **k: cfg)
        return main_mod

    def _args(self, **kw):
        import argparse

        defaults = {"json": True, "name": None, "all": False}
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_up_clears_disabled_and_starts(self, cli, capsys, monkeypatch):
        started = []
        monkeypatch.setattr(
            services, "start_service", lambda svc: (started.append(svc.name) or True, "started")
        )
        services.set_disabled("tracker", True)
        assert cli.cmd_services_up(self._args(name="tracker")) == 0
        assert started == ["tracker"]
        assert "tracker" not in services.load_disabled()
