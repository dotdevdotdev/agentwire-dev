"""Tests for agentwire/services.py — registry, healthchecks, watchdog policy (#214)."""

import json
from pathlib import Path

import pytest

from agentwire import services
from agentwire.config import (
    Config,
    CustomServiceConfig,
    HealthcheckConfig,
    ServicesConfig,
    _dict_to_config,
)
from agentwire.services import WatchdogState, BACKOFF_BASE, BACKOFF_CAP


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "services-state.json"
    monkeypatch.setattr(services, "STATE_FILE", f)
    return f


class TestDisabledState:
    def test_empty_when_missing(self, state_file):
        assert services.load_disabled() == set()

    def test_round_trip(self, state_file):
        services.set_disabled("work-tracker", True)
        assert services.load_disabled() == {"work-tracker"}
        services.set_disabled("work-tracker", False)
        assert services.load_disabled() == set()

    def test_corrupt_file_treated_as_empty(self, state_file):
        state_file.write_text("not json {")
        assert services.load_disabled() == set()


class TestConfigParsing:
    """healthcheck/restart fields parse; string shorthand keeps working."""

    def test_full_dict_entry(self):
        cfg = _dict_to_config({"services": {"custom": [{
            "name": "tracker",
            "project": "/tmp/tracker",
            "restart": "never",
            "healthcheck": {"kind": "http", "url": "http://x/health", "interval": 30},
        }]}})
        svc = cfg.services.custom[0]
        assert svc.restart == "never"
        assert svc.healthcheck.kind == "http"
        assert svc.healthcheck.url == "http://x/health"
        assert svc.healthcheck.interval == 30

    def test_defaults(self):
        cfg = _dict_to_config({"services": {"custom": [{"name": "tracker"}]}})
        svc = cfg.services.custom[0]
        assert svc.restart == "on-failure"
        assert svc.healthcheck.kind == "tmux_session"
        assert svc.healthcheck.interval == 60

    def test_string_shorthand(self):
        cfg = _dict_to_config({"services": {"custom": ["tracker"]}})
        svc = cfg.services.custom[0]
        assert svc.name == "tracker"
        assert svc.restart == "on-failure"
        assert svc.healthcheck.kind == "tmux_session"


class TestRegistry:
    def _cfg(self, custom):
        return Config(services=ServicesConfig(custom=custom))

    def test_builtin_notifications_synthesized(self, monkeypatch):
        monkeypatch.setattr(services, "notifications_session_name",
                            lambda: "agentwire-notifications")
        reg = services.registry(self._cfg([]))
        assert reg[0].name == "agentwire-notifications"
        assert reg[0].roles == "notifications"
        assert reg[0].type == "claude-bypass"
        assert reg[0].restart == "on-failure"

    def test_user_services_appended(self, monkeypatch):
        monkeypatch.setattr(services, "notifications_session_name",
                            lambda: "agentwire-notifications")
        user = CustomServiceConfig(name="tracker")
        reg = services.registry(self._cfg([user]))
        assert [s.name for s in reg] == ["agentwire-notifications", "tracker"]

    def test_user_override_replaces_builtin(self, monkeypatch):
        monkeypatch.setattr(services, "notifications_session_name",
                            lambda: "agentwire-notifications")
        override = CustomServiceConfig(name="agentwire-notifications", restart="never")
        reg = services.registry(self._cfg([override]))
        assert len(reg) == 1
        assert reg[0].restart == "never"


class TestRunHealthcheck:
    def test_command_exit_zero_healthy(self):
        svc = CustomServiceConfig(name="x", healthcheck=HealthcheckConfig(
            kind="command", command="true"))
        healthy, detail = services.run_healthcheck(svc)
        assert healthy is True

    def test_command_exit_nonzero_unhealthy(self):
        svc = CustomServiceConfig(name="x", healthcheck=HealthcheckConfig(
            kind="command", command="false"))
        healthy, detail = services.run_healthcheck(svc)
        assert healthy is False
        assert "exit 1" in detail

    def test_command_missing_command(self):
        svc = CustomServiceConfig(name="x", healthcheck=HealthcheckConfig(kind="command"))
        healthy, detail = services.run_healthcheck(svc)
        assert healthy is False and "requires command" in detail

    def test_http_missing_url(self):
        svc = CustomServiceConfig(name="x", healthcheck=HealthcheckConfig(kind="http"))
        healthy, detail = services.run_healthcheck(svc)
        assert healthy is False and "requires url" in detail

    def test_tmux_session_kind(self, monkeypatch):
        svc = CustomServiceConfig(name="x")  # default tmux_session
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        assert services.run_healthcheck(svc) == (True, "session exists")
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        assert services.run_healthcheck(svc) == (False, "session not found")


class TestStartAllAutostart:
    def _cfg(self, custom):
        return Config(services=ServicesConfig(custom=custom))

    def test_skips_disabled_and_non_autostart(self, state_file, monkeypatch):
        monkeypatch.setattr(services, "notifications_session_name", lambda: "notif")
        started = []
        monkeypatch.setattr(services, "start_service",
                            lambda svc: (started.append(svc.name) or True, "started"))
        services.set_disabled("downed", True)
        cfg = self._cfg([
            CustomServiceConfig(name="downed"),
            CustomServiceConfig(name="manual", autostart=False),
            CustomServiceConfig(name="normal"),
        ])
        results = services.start_all_autostart(cfg)
        assert started == ["notif", "normal"]
        by_name = {r["name"]: r for r in results}
        assert by_name["downed"]["skipped"].startswith("disabled")
        assert by_name["manual"]["skipped"] == "autostart off"
        assert by_name["normal"]["ok"] is True


class TestWatchdogState:
    """The pure restart/notify policy — the heart of the watchdog."""

    def test_first_check_healthy_no_actions(self):
        s = WatchdogState()
        assert s.on_check(1000, True, "on-failure") == []

    def test_transition_to_down_notifies_and_restarts(self):
        s = WatchdogState()
        s.on_check(1000, True, "on-failure")
        actions = s.on_check(1060, False, "on-failure")
        assert actions == ["notify_down", "restart"]

    def test_first_ever_check_unhealthy_notifies(self):
        s = WatchdogState()
        assert "notify_down" in s.on_check(1000, False, "on-failure")

    def test_recovery_notifies_once(self):
        s = WatchdogState()
        s.on_check(1000, False, "on-failure")
        actions = s.on_check(1060, True, "on-failure")
        assert actions == ["notify_recovered"]
        # Staying healthy is quiet
        assert s.on_check(1120, True, "on-failure") == []

    def test_never_policy_notifies_but_never_restarts(self):
        s = WatchdogState()
        for t in (1000, 1060, 1120):
            actions = s.on_check(t, False, "never")
            assert "restart" not in actions
        # Only the first failure notified
        assert s.healthy is False

    def test_backoff_grows_and_caps(self):
        s = WatchdogState()
        now = 1000.0
        delays = []
        # Drive repeated failures, always past the backoff gate
        for _ in range(8):
            actions = s.on_check(now, False, "on-failure")
            assert "restart" in actions
            delays.append(s.next_restart_at - now)
            now = s.next_restart_at  # jump exactly to the next allowed attempt
        assert delays[0] == BACKOFF_BASE
        assert delays[1] == BACKOFF_BASE * 2
        assert delays[-1] == BACKOFF_CAP
        assert max(delays) == BACKOFF_CAP

    def test_no_restart_while_backing_off(self):
        s = WatchdogState()
        s.on_check(1000, False, "on-failure")  # restart, next at 1030
        actions = s.on_check(1010, False, "on-failure")
        assert actions == []  # still down, not yet allowed to retry

    def test_recovery_resets_backoff(self):
        s = WatchdogState()
        for _ in range(4):
            s.on_check(1000 + s.next_restart_at, False, "on-failure")
        s.on_check(5000, True, "on-failure")
        assert s.restart_count == 0 and s.next_restart_at == 0.0
        actions = s.on_check(5060, False, "on-failure")
        assert "restart" in actions
        assert s.next_restart_at - 5060 == BACKOFF_BASE

    def test_always_policy_behaves_like_on_failure(self):
        s = WatchdogState()
        actions = s.on_check(1000, False, "always")
        assert actions == ["notify_down", "restart"]


class TestServiceStatus:
    def test_status_shape(self, state_file, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        svc = CustomServiceConfig(name="tracker", project="/tmp/x")
        status = services.service_status(svc)
        assert status["name"] == "tracker"
        assert status["running"] is True
        assert status["healthy"] is True
        assert status["disabled"] is False
        assert status["restart"] == "on-failure"
        assert status["healthcheck"] == {"kind": "tmux_session", "interval": 60}

    def test_disabled_flag(self, state_file, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        services.set_disabled("tracker", True)
        status = services.service_status(CustomServiceConfig(name="tracker"))
        assert status["disabled"] is True and status["healthy"] is False


class TestServicesCLI:
    """cmd_services_* — JSON contracts the portal watchdog depends on."""

    @pytest.fixture
    def cli(self, state_file, monkeypatch):
        from agentwire import __main__ as main_mod
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

    def test_list_json(self, cli, capsys):
        assert cli.cmd_services_list(self._args()) == 0
        data = json.loads(capsys.readouterr().out)
        assert [s["name"] for s in data["services"]] == ["notif", "tracker"]

    def test_status_json_always_exit_zero(self, cli, capsys, monkeypatch):
        # Even with everything down, JSON mode must exit 0 — the watchdog
        # needs the payload precisely when services are unhealthy.
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        assert cli.cmd_services_status(self._args()) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["all_healthy"] is False

    def test_up_clears_disabled_and_starts(self, cli, capsys, monkeypatch):
        started = []
        monkeypatch.setattr(services, "start_service",
                            lambda svc: (started.append(svc.name) or True, "started"))
        services.set_disabled("tracker", True)
        assert cli.cmd_services_up(self._args(name="tracker")) == 0
        assert started == ["tracker"]
        assert "tracker" not in services.load_disabled()

    def test_down_disables_then_stops(self, cli, capsys, monkeypatch):
        calls = []
        monkeypatch.setattr(services, "stop_service",
                            lambda name: (calls.append(("stop", name, "tracker" in services.load_disabled())) or True, "stopped"))
        assert cli.cmd_services_down(self._args(name="tracker")) == 0
        # Disabled flag was already set when stop ran (no watchdog race)
        assert calls == [("stop", "tracker", True)]
        assert "tracker" in services.load_disabled()

    def test_unknown_service(self, cli, capsys):
        assert cli.cmd_services_up(self._args(name="nope")) == 1
