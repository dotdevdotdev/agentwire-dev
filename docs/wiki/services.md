# Custom Services

> Living document. Update this, don't create new versions.

A **custom service** is a long-running agentwire session you register once and never babysit again: it boots when the portal boots (including after a reboot), the portal watchdog health-checks it, and a dead service gets a toast + TTS alert and an automatic respawn with backoff. Examples: a work-tracker session that receives `/log-work` pushes, a monitoring agent, a cron-companion session.

The notifications bridge (`agentwire-notifications`, the idle-nag TTS session) is a **built-in registry entry** — it gets the same lifecycle, with no bespoke code path.

## Registering a service

`services.custom` in `~/.agentwire/config.yaml`:

```yaml
services:
  custom:
    - name: work-tracker             # tmux session name (required)
      project: ~/projects/tracker    # project dir (default: dev source dir)
      type: claude-bypass            # optional session-type override
      roles: tracker                 # optional roles override (comma-separated)
      autostart: true                # boot on portal launch / `agentwire up` (default)
      restart: on-failure            # never | on-failure | always (default on-failure)
      healthcheck:
        kind: tmux_session           # tmux_session (default) | http | command
        interval: 60                 # seconds between watchdog checks
    - "simple-service"               # string shorthand: name only, all defaults
```

Healthcheck kinds:

| Kind | Healthy when |
|------|--------------|
| `tmux_session` (default) | the tmux session exists |
| `http` | GET `url` returns 2xx |
| `command` | `command` exits 0 (10s timeout) |

## Lifecycle

```
portal launch ──► services up --all ──► watchdog (every interval)
                  (autostart, skips        │
                   downed services)        ├─ healthy ──────────── quiet
                                           ├─ goes down ─────────► toast + TTS, respawn
                                           │                       (backoff 30s→10m)
                                           └─ recovers ──────────► toast
```

- **Autostart** happens in the portal server itself (`run_server()`), so every start path converges: a reboot via the launchd plist (`agentwire portal start`), `agentwire portal restart`, and `agentwire up` all bring services back. No separate step.
- **Watchdog** (`service_watchdog_loop` in server.py) checks each service on its `interval`. Failure → toast + TTS on the transition, then respawns per `restart` policy with exponential backoff (30s, 60s, ... capped at 10m; reset on recovery). `restart: never` only notifies. `always` behaves like `on-failure` for tmux services.
- **Manual stop sticks**: `agentwire services down <name>` records the service as disabled in `~/.agentwire/services-state.json` *before* killing it — neither the watchdog nor `up --all` resurrects it until `agentwire services up <name>`.

## CLI

```bash
agentwire services list           # registry + autostart/restart/healthcheck/disabled
agentwire services status        # run healthchecks now; exit 1 if something's down
agentwire services status NAME   # one service
agentwire services up NAME       # start (clears 'down' state)
agentwire services up --all      # all autostart services (skips downed)
agentwire services down NAME     # stop and keep stopped
```

`--json` everywhere. Note: `status --json` always exits 0 — the payload carries `all_healthy`, and machine consumers (the watchdog) need the data precisely when something is unhealthy.

MCP (read-only introspection for agents): `services_list()`, `services_status()`.

`agentwire doctor` includes a registry-driven services section: `[ok]` healthy, `[!!]` should-be-running-but-isn't (with the fix command), `[..]` downed or autostart-off.

## Internals

Single source of truth: `agentwire/services.py` — registry synthesis (built-ins + config), healthcheck runners, start/stop, disabled-state file, and the pure `WatchdogState` policy class (unit-tested backoff/notify matrix). The CLI commands wrap it; `agentwire up` and the portal's autostart + watchdog call the CLI (`services up --all`, `services status`), never duplicate the logic.

Portal Services column: the sidebar fetches `/api/services/custom` and groups those session names under Services automatically.

Deliberately out of scope (for now): per-project services in `.agentwire.yml`, sidebar health badges.
