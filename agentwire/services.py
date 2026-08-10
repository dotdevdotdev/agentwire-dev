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
import re
import subprocess
import sys
import time
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


# Built-in infrastructure sessions — the Python mirror of the frontend's
# SERVICE_SESSIONS allowlist (static/js/service-classification.js). These are
# long-lived services, not delegated work, so they must never fire a
# "child finished" idle notification at a parent (the idle-nag bridge alone
# cycles active→idle ~1440×/day). Kept in lockstep with the JS set.
_BUILTIN_SERVICE_SESSIONS = frozenset({
    "agentwire-portal",
    "agentwire-tts",
    "agentwire-stt",
    "agentwire-kokoro",
    "agentwire-scheduler",
})


def is_service_session(name: str) -> bool:
    """Is *name* an infrastructure service session (vs. a working session)?

    True for the built-in infra sessions, the configurable idle-nag bridge, and
    any user-defined custom service. Used to suppress idle parent-notifications
    for services — a worktree / `agentwire new` child is NOT a service and still
    notifies. Name-only and dependency-light so the idle hook can call it cheaply
    on every idle event.
    """
    bare = (name or "").split("@")[0]
    if not bare:
        return False
    if bare in _BUILTIN_SERVICE_SESSIONS or bare == notifications_session_name():
        return True
    try:
        from .config import load_config

        return any(s.name == bare for s in load_config().services.custom)
    except Exception:
        return False


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
        posture="bypass",
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


#: How long a freshly spawned process gets to prove it survived exec. Short
#: enough that `services up` stays interactive, long enough to catch the class
#: this exists for: a missing key, an unregistered name, a bad flag — failures
#: that happen at startup, not later.
STARTUP_GRACE_S = 0.6

#: Lines of a dead pane's output carried into the failure message. Bounded on
#: purpose: this is the process's OWN stderr being surfaced to the operator,
#: and not only on a terminal — the detail built from it is toasted and SPOKEN
#: by the portal watchdog. The trade is deliberate (a refusal that cannot say
#: why is a fix-loop, and in a channel with no screen that is the expensive
#: failure), which is why the content is redacted, not just clipped.
_TAIL_LINES = 3

#: ...and a separate cap, because ``_TAIL_LINES`` bounds LINES, not characters:
#: three lines of a 5000-column traceback is one utterance nobody can listen to.
_TAIL_CHARS = 300


def _tmux_name(name: str) -> str:
    """The name tmux will actually have chosen for *name*.

    tmux rewrites its own address separators — ``.`` and ``:`` → ``_`` — so a
    service called ``rev.dot:2`` becomes ``rev_dot_2`` the moment it is
    created, and every later target built from the raw name misses. That is
    #868/#878, and the mapping has exactly ONE implementation by rule: never
    inline the substitution, call the helper.
    """
    from .worktree import tmux_safe_name

    return tmux_safe_name(name)


def _tmux_session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={_tmux_name(name)}"],
        capture_output=True,
    )
    return result.returncode == 0


def _tmux_pane_dead(name: str) -> bool:
    """Is *name*'s pane a corpse tmux is holding open?

    Only ever true under ``remain-on-exit`` — which command services now set,
    and which a user may have set globally for anything else. Measured, and the
    reason this predicate has to exist: ``tmux has-session`` returns 0 for a
    session whose pane is dead, so "the session exists" stopped being liveness
    the moment anything kept a corpse around.
    """
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", f"={_tmux_name(name)}:.0",
         "#{pane_dead}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def _tmux_pane_tail(name: str) -> str:
    """The last few non-empty lines of *name*'s pane, redacted, or "".

    Read from tmux's in-memory scrollback — no file is created, so the command
    kind's secret property is unchanged. tmux's own "Pane is dead (status N)"
    line is kept: the exit status is often the most actionable part.

    Redacted HERE because this is the one choke point every consumer reads
    through, and the consumers are not all a terminal: the healthcheck detail
    built from this reaches the portal watchdog's ``_notify_service_event``,
    which toasts it AND speaks it via ``agentwire say``. A crash line is
    exactly where a secret shows up, and "it only goes where its own log would
    have" was not true of a spoken utterance.
    """
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-S", "-", "-t", f"={_tmux_name(name)}:.0"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    # Redact BEFORE clipping, and the reason is legibility, not safety: clipping
    # first is equally SAFE (a cut can only remove trailing material, and the
    # key stays in front of whatever value survives, so the pattern still
    # matches) — but a 400-character token would then eat the whole budget and
    # push the actionable part of the message off the end. Redaction shortens
    # `--token=<400 chars>` to `--token=***`, so doing it first spends the cap
    # on the words the operator needs.
    tail = redact_secrets(" | ".join(lines[-_TAIL_LINES:]))
    return tail if len(tail) <= _TAIL_CHARS else tail[:_TAIL_CHARS] + "…"


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
    # Default: tmux_session — "the session exists AND its pane is not a corpse".
    # The second clause is not belt-and-braces: `has-session` returns 0 for a
    # dead pane, so without it a crashed service reads healthy forever, which
    # is a worse failure than the one at start it would be masking.
    if not _tmux_session_exists(svc.name):
        return False, "session not found"
    if _tmux_pane_dead(svc.name):
        tail = _tmux_pane_tail(svc.name)
        return False, f"process exited: {tail}" if tail else "process exited"
    return True, "session exists"


def service_kind(svc: CustomServiceConfig) -> str:
    """"command" (a supervised process) or "agent" (an agentwire session)."""
    return "command" if svc.command else "agent"


def service_status(svc: CustomServiceConfig) -> dict:
    """Full status for one service (runs its healthcheck now)."""
    healthy, detail = run_healthcheck(svc)
    return {
        "name": svc.name,
        "kind": service_kind(svc),
        "running": _tmux_session_exists(svc.name) and not _tmux_pane_dead(svc.name),
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


def _start_command_service(svc: CustomServiceConfig) -> tuple[bool, str]:
    """Run a plain process under tmux, detached. Idempotent.

    tmux IS the supervisor here, and that choice is what keeps a process
    service's output off any world-readable surface: tmux captures stdout and
    stderr into the pane's scrollback, which lives in the tmux server's memory
    and is reachable only through the per-user socket dir ``/tmp/tmux-<uid>``
    (mode 0700). Nothing is redirected to a file — deliberately. A wrapper that
    is careful in its own code and then tees stdout into a log has not solved
    the problem, and the same standard #887 holds ``~/.agentwire`` and
    ``portal.token`` to applies here: owner-only or not at all.

    What this does NOT hide is ``command`` itself: it lands in the process
    table, where every local user can read it. Secrets belong in
    ``~/.agentwire/.env``, never in a service's argv — see
    ``command_secret_risk``.

    **A created pane is not a surviving process.** ``tmux new-session``
    returning 0 says a pane was made; the command inside it may already have
    exited. Reporting that as "started" produced the worst shape available: a
    success line, a ``[!!] unhealthy`` from doctor one second later prescribing
    the command that had just claimed success, and the process's own stderr
    gone with the pane. So the spawn is three steps rather than one — a
    placeholder, ``remain-on-exit on``, then the real command via
    ``respawn-pane`` — and then the pane is re-read after a grace period.

    The ordering is the point and is why a placeholder exists at all: setting
    the option AFTER launching the real command is a race a fast-dying process
    wins, and it is exactly the fast-dying processes whose reason we need.

    **The placeholder must never outlive the spawn.** Steps 2 and 3 run against
    a session step 1 just created, so a failure there is not the benign
    "someone else got there first" the old handler assumed — it is our own
    placeholder, alive and running ``sleep 3600``. ``pane_dead`` cannot tell
    that from a healthy process, so leaving it would report a sleep loop as a
    running service: the same false all-clear one costume over. ``already
    running`` is therefore reachable only when the session genuinely
    PRE-EXISTED this call.
    """
    cwd = svc.project or str(Path.home())
    # Every -s and -t goes through the one mapping. tmux rewrites `.` and `:`
    # at creation, so a target built from the raw name misses the session that
    # was actually made — and a teardown that misses reports success (#868).
    name = _tmux_name(svc.name)
    prior = ""
    if _tmux_session_exists(svc.name):
        if not _tmux_pane_dead(svc.name):
            return True, "already running"
        # A corpse from a previous run. Read WHY before clearing it — the
        # respawn destroys the only copy of that output, and this is the moment
        # the operator is asking about it.
        tail = _tmux_pane_tail(svc.name)
        prior = f" (previous run exited: {tail})" if tail else " (previous run exited)"
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={name}"],
            capture_output=True, timeout=30,
        )

    created_here = False
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", cwd,
             "sh -c 'while :; do sleep 3600; done'"],
            check=True, capture_output=True, timeout=30,
        )
        created_here = True
        subprocess.run(
            ["tmux", "set-option", "-w", "-t", f"={name}:", "remain-on-exit", "on"],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["tmux", "respawn-pane", "-k", "-c", cwd, "-t", f"={name}:.0",
             svc.command],
            check=True, capture_output=True, timeout=30,
        )
    except Exception as e:
        if created_here:
            # Ours, and it is only running the placeholder. Take it with us.
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={name}"],
                capture_output=True, timeout=30,
            )
        elif _tmux_session_exists(svc.name) and not _tmux_pane_dead(svc.name):
            return True, "already running"  # genuinely lost a benign spawn race
        stderr = ""
        if isinstance(e, subprocess.CalledProcessError):
            stderr = (e.stderr or b"").decode(errors="replace").strip()
        return False, (stderr or str(e)) + prior

    time.sleep(STARTUP_GRACE_S)
    if _tmux_pane_dead(svc.name):
        tail = _tmux_pane_tail(svc.name)
        # The pane is deliberately LEFT dead: it is the only place that output
        # exists, and the next start reads it before clearing it.
        return False, f"process exited immediately: {tail}" if tail else (
            "process exited immediately")
    return True, "started" + prior


# Argv patterns that read as an inline secret. The process table is world-
# readable, so a service command carrying one hands it to every local user —
# the one leak a tmux-supervised process service still has.
#
# Both joinings, because a flag and its value are the same secret either way:
# `--token=abc` and `--token abc` reach `ps` identically, and matching only the
# first would select for whoever writes it the other way — the same class of
# spelling-shaped rule this repo has been bitten by before (#913/#915).
_SECRET_NAMES = ("token", "api-key", "apikey", "api_key", "secret", "password", "passwd")
_SECRET_ARGV_PATTERNS = tuple(
    pattern
    for name in _SECRET_NAMES
    for pattern in (f"--{name}=", f"--{name} ", f"{name}=")
) + ("bearer ",)


#: Built from the same tuple as the argv check, deliberately: a second list
#: would drift from it the moment either is extended, which is exactly what
#: happened to the argv one.
_SECRET_VALUE_RE = re.compile(
    "(" + "|".join(re.escape(p) for p in _SECRET_ARGV_PATTERNS) + r")(\S+)",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Mask values that follow a secret-shaped key in *text*.

    Masks the VALUE and keeps everything else — a redaction that ate the
    message would re-create the failure it is guarding, which is a refusal that
    cannot say why.
    """
    return _SECRET_VALUE_RE.sub(lambda m: m.group(1) + "***", text)


def command_secret_risk(svc: CustomServiceConfig) -> str | None:
    """The inline-secret pattern *svc*'s command carries, or None.

    Detection only, and named rather than guessed at: the caller decides what
    to do about it. A false positive here costs a doctor line; a miss costs a
    credential.
    """
    if not svc.command:
        return None
    lowered = svc.command.lower()
    for pattern in _SECRET_ARGV_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def start_service(svc: CustomServiceConfig) -> tuple[bool, str]:
    """Start a service (detached) if not already running. Idempotent.

    A `command` service is a supervised process; everything else is an
    agentwire agent session.
    """
    if svc.command:
        return _start_command_service(svc)
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
    if svc.posture:
        cmd.extend(["--posture", svc.posture])
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


def stop_service(svc: CustomServiceConfig) -> tuple[bool, str]:
    """Kill a service's tmux session.

    A command service is killed through tmux directly: `agentwire kill`'s
    graceful leg sends `/exit` to an *agent*, and there is no agent here — a
    process would just be handed two characters it never asked for before the
    kill landed anyway.
    """
    if not _tmux_session_exists(svc.name):
        return True, "not running"
    try:
        if svc.command:
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={_tmux_name(svc.name)}"],
                check=True, capture_output=True, timeout=30,
            )
        else:
            subprocess.run(
                [sys.executable, "-m", "agentwire", "kill", "-s", svc.name, "--json"],
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
