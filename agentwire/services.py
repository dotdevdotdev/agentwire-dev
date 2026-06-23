"""Custom service registry, health checks, and watchdog policy.

A custom service is a long-running agentwire session registered in
``services.custom`` in ``~/.agentwire/config.yaml``. This module is the
single source of truth for service lifecycle logic — the CLI commands
(``agentwire services ...``), ``agentwire up``, ``agentwire doctor``, and
the portal's autostart + watchdog all call into it.

State: ``~/.agentwire/services-state.json`` records services the user has
manually stopped (``agentwire services down``) so neither ``up --all`` nor
the watchdog resurrects them.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Config, CustomServiceConfig, HealthcheckConfig

STATE_FILE = Path.home() / ".agentwire" / "services-state.json"

# Watchdog restart backoff: 30s, 60s, 120s, ... capped at 10 minutes.
BACKOFF_BASE = 30
BACKOFF_CAP = 600


# ─────────────────────────────────────────────────────────────
# Disabled-state file (manual `services down` must stick)
# ─────────────────────────────────────────────────────────────


def load_disabled() -> set[str]:
    """Names of services manually stopped via `agentwire services down`."""
    try:
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("disabled", []))
    except (OSError, json.JSONDecodeError):
        return set()


def set_disabled(name: str, disabled: bool) -> None:
    """Add/remove a service from the disabled set."""
    current = load_disabled()
    if disabled:
        current.add(name)
    else:
        current.discard(name)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"disabled": sorted(current)}, indent=2))


# ─────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────


def _raw_services_config() -> dict:
    """Raw `services:` mapping from config.yaml (for fields the typed
    Config doesn't carry, e.g. notifications session_name)."""
    try:
        config_path = Path.home() / ".agentwire" / "config.yaml"
        data = yaml.safe_load(config_path.read_text()) or {}
        return data.get("services", {}) or {}
    except (OSError, yaml.YAMLError):
        return {}


def notifications_session_name() -> str:
    """The idle-nag TTS bridge session name (configurable, rarely changed)."""
    return (
        _raw_services_config()
        .get("notifications", {})
        .get("session_name", "agentwire-notifications")
    )


def _source_dir() -> str:
    """agentwire source dir (dev.source_dir), project for built-in services."""
    try:
        config_path = Path.home() / ".agentwire" / "config.yaml"
        data = yaml.safe_load(config_path.read_text()) or {}
        source = data.get("dev", {}).get("source_dir", "~/projects/agentwire-dev")
    except (OSError, yaml.YAMLError):
        source = "~/projects/agentwire-dev"
    return str(Path(source).expanduser())


def registry(cfg: Config) -> list[CustomServiceConfig]:
    """All managed services: built-ins first, then user-defined.

    The notifications session (idle-nag TTS bridge) is a built-in registry
    entry — same lifecycle as user services (autostart, watchdog, doctor).
    A user-defined service with the same name overrides the built-in.
    """
    notif_name = notifications_session_name()
    user_services = list(cfg.services.custom)
    if any(s.name == notif_name for s in user_services):
        return user_services

    notifications = CustomServiceConfig(
        name=notif_name,
        project=_source_dir(),
        autostart=True,
        roles="notifications",
        type="claude-bypass",
        restart="on-failure",
        healthcheck=HealthcheckConfig(),  # tmux_session, 60s
        # Default-on context auto-management (issue #442): the idle-nag bridge
        # is STATELESS — it's fed ~1440 [IDLE NAG] prompts/day and needs none of
        # its backlog, so /clear it aggressively when it bloats rather than
        # leaning on Claude's own (stateful-oriented) auto-compaction.
        context_policy="clear",
    )
    return [notifications, *user_services]


# ─────────────────────────────────────────────────────────────
# Health checks
# ─────────────────────────────────────────────────────────────


def _tmux_session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"],
        capture_output=True,
    )
    return result.returncode == 0


def run_healthcheck(svc: CustomServiceConfig) -> tuple[bool, str]:
    """Probe a service's health. Returns (healthy, detail)."""
    hc = svc.healthcheck
    if hc.kind == "http":
        if not hc.url:
            return False, "healthcheck kind 'http' requires url"
        from .tunnels import test_service_health
        healthy, error = test_service_health(hc.url, timeout=5)
        return healthy, error or "2xx"
    if hc.kind == "command":
        if not hc.command:
            return False, "healthcheck kind 'command' requires command"
        try:
            result = subprocess.run(
                hc.command, shell=True, capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                return True, "exit 0"
            return False, f"exit {result.returncode}"
        except subprocess.TimeoutExpired:
            return False, "healthcheck timed out (10s)"
        except Exception as e:
            return False, str(e)
    # Default: tmux_session
    if _tmux_session_exists(svc.name):
        return True, "session exists"
    return False, "session not found"


def service_status(svc: CustomServiceConfig) -> dict:
    """Full status for one service (runs its healthcheck now)."""
    healthy, detail = run_healthcheck(svc)
    return {
        "name": svc.name,
        "running": _tmux_session_exists(svc.name),
        "healthy": healthy,
        "detail": detail,
        "disabled": svc.name in load_disabled(),
        "autostart": svc.autostart,
        "restart": svc.restart,
        "healthcheck": {"kind": svc.healthcheck.kind, "interval": svc.healthcheck.interval},
        "project": svc.project,
    }


# ─────────────────────────────────────────────────────────────
# Start / stop
# ─────────────────────────────────────────────────────────────


def start_service(svc: CustomServiceConfig) -> tuple[bool, str]:
    """Start a service session (detached) if not already running. Idempotent."""
    if _tmux_session_exists(svc.name):
        return True, "already running"

    project = svc.project or _source_dir()
    # --allow-shared-dir: registering a service is explicit intent — skip the
    # guard that refuses a second session on a project dir with active sessions
    # (the built-in notifications entry shares the source dir with portal/tts).
    # Deliberately NOT --force: concurrent spawns (autostart + watchdog + manual
    # `services up`) must degrade to a harmless "already exists", never
    # kill-replace a healthy instance that won the race.
    cmd = [sys.executable, "-m", "agentwire", "new", "-s", svc.name, "-p", project,
           "--allow-shared-dir", "--json"]
    if svc.roles:
        cmd.extend(["--roles", svc.roles])
    if svc.type:
        cmd.extend(["--type", svc.type])
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        return True, "started"
    except subprocess.CalledProcessError as e:
        if _tmux_session_exists(svc.name):
            return True, "already running"  # lost a benign spawn race
        stderr = (e.stderr or b"").decode(errors="replace").strip()
        return False, stderr or str(e)
    except Exception as e:
        return False, str(e)


def stop_service(name: str) -> tuple[bool, str]:
    """Kill a service's tmux session."""
    if not _tmux_session_exists(name):
        return True, "not running"
    try:
        subprocess.run(
            [sys.executable, "-m", "agentwire", "kill", "-s", name, "--json"],
            check=True, capture_output=True, timeout=30,
        )
        return True, "stopped"
    except Exception as e:
        return False, str(e)


def start_all_autostart(cfg: Config) -> list[dict]:
    """Start every `autostart: true` service that isn't manually disabled.

    The shared boot path for `agentwire up` AND portal launch. Idempotent.
    Returns per-service results.
    """
    disabled = load_disabled()
    results = []
    for svc in registry(cfg):
        if not svc.autostart:
            results.append({"name": svc.name, "skipped": "autostart off"})
            continue
        if svc.name in disabled:
            results.append({"name": svc.name, "skipped": "disabled (services down)"})
            continue
        ok, msg = start_service(svc)
        results.append({"name": svc.name, "ok": ok, "result": msg})
    return results


# ─────────────────────────────────────────────────────────────
# Watchdog policy (pure — the portal's loop feeds it check results)
# ─────────────────────────────────────────────────────────────


@dataclass
class WatchdogState:
    """Per-service restart/notify policy state.

    `on_check` is called by the watchdog after each healthcheck and returns
    the actions to take: notify_down / notify_recovered fire only on
    transitions; restart is gated by exponential backoff and the service's
    restart policy ("never" only notifies; "on-failure"/"always" respawn).
    """

    healthy: bool | None = None   # None until first check
    restart_count: int = 0
    next_restart_at: float = 0.0

    def on_check(self, now: float, healthy: bool, restart_policy: str) -> list[str]:
        actions: list[str] = []
        was_healthy = self.healthy
        self.healthy = healthy

        if healthy:
            if was_healthy is False:
                actions.append("notify_recovered")
            self.restart_count = 0
            self.next_restart_at = 0.0
            return actions

        # Unhealthy
        if was_healthy is not False:  # transition (or first-ever check)
            actions.append("notify_down")

        if restart_policy in ("on-failure", "always") and now >= self.next_restart_at:
            actions.append("restart")
            backoff = min(BACKOFF_BASE * (2 ** self.restart_count), BACKOFF_CAP)
            self.restart_count += 1
            self.next_restart_at = now + backoff

        return actions
