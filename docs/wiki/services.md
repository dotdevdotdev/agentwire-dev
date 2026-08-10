# Custom Services

> Living document. Update this, don't create new versions.

A **custom service** is something long-running you register once and never babysit again: it boots when the portal boots (including after a reboot), the portal watchdog health-checks it, and a dead service gets a toast + TTS alert and an automatic respawn with backoff. Examples: a work-tracker session that receives `/log-work` pushes, a monitoring agent, a cron-companion session, a local bridge process.

The notifications bridge (`agentwire-notifications`, the idle-nag TTS session) is a **built-in registry entry** — it gets the same lifecycle, with no bespoke code path.

## Two kinds

A service is an **agent** or a **command**, and the only thing that decides is whether the entry sets `command:`.

| | agent (no `command`) | command (`command:` set) |
|---|---|---|
| What runs | an agentwire session (`agentwire new`) | your process, in a detached tmux session |
| `roles` / `posture` / `context_policy` | apply | **rejected** — there is no agent to carry them |
| Stopped by | `agentwire kill` (graceful `/exit` first) | `tmux kill-session` |
| Default healthcheck | session exists **and** its pane is not dead | same |
| A failed start | `agentwire new` exits non-zero | the pane is re-read after a grace period |

The command kind is deliberately generic. agentwire supervises a process; it does not know or care what the process is.

## A created pane is not a surviving process

`tmux new-session` returning 0 says a pane was made. The command inside it may already have exited — a missing key, a bad flag, a name that isn't registered — and reporting that as `started` produces the worst shape available: a success line, then `[!!] unhealthy` from doctor one second later prescribing the command that just claimed success, with the process's own stderr gone along with the pane. Screenless, that is a fix-loop behind an all-clear.

So a command service is spawned in three steps — a placeholder, `remain-on-exit on`, then the real command via `respawn-pane` — and the pane is re-read after `STARTUP_GRACE_S`. The ordering is why the placeholder exists at all: setting the option *after* launching the real command is a race that a fast-dying process wins, and fast-dying processes are exactly the ones whose reason is needed. A process that died reports `process exited immediately: <its last lines>`, and the corpse is deliberately left in place — tmux's memory is the only copy of that output, and the next start reads it before clearing it.

Two consequences worth stating plainly:

- **`remain-on-exit` means `has-session` is no longer liveness.** Measured: `tmux has-session` returns 0 for a session whose pane is dead. So the `tmux_session` healthcheck asks `#{pane_dead}` too, for *both* kinds — `remain-on-exit` is a user tmux setting, so an agent session could always have been in this state and was reported healthy.
- **A dead pane is not "already running".** A start that found a corpse clears it and respawns, or the watchdog would loop forever: healthcheck says unhealthy, watchdog starts, start says "already running".
- **Neither is a placeholder.** Steps 2 and 3 run against a session step 1 just created, so a failure there is *our* placeholder — alive, running `sleep 3600`, and indistinguishable from a healthy process to `pane_dead`. It is killed and the start fails; `already running` is reachable only when the session genuinely pre-existed the call.
- **Every `-s` and `-t` goes through `worktree.tmux_safe_name`.** tmux rewrites `.` and `:` to `_` at creation, so a target built from the raw name misses the session that was actually made — the spawn has five targets, and a teardown that misses reports success while the session survives (#868/#878). That mapping has one implementation by rule; never inline it.

### Where a crash line actually goes, and why it is redacted

The process's last lines end up in the start message and in the healthcheck `detail` — and `detail` does not stay on a terminal. The portal watchdog passes it to `_notify_service_event`, which **toasts it in the browser and speaks it via `agentwire say`**. So a process printing `bearer eyJ…` as it died would have put that verbatim into a spoken utterance.

Everything the pane yields is therefore run through `redact_secrets` at the single point every consumer reads through (`_tmux_pane_tail`), using the *same* pattern set as the argv check — one source of truth, because a second list drifts from the first the moment either is extended. The value is masked and the rest of the line kept: a redaction that ate the message would re-create the failure it guards.

Two bounds, and they are different: `_TAIL_LINES` (3) bounds **lines**, `_TAIL_CHARS` (300) bounds **characters** — three lines of a 5000-column traceback is one utterance nobody can listen to. Redaction runs *before* the clip for legibility, not safety: clipping first is equally safe (a cut only removes trailing material, and the key stays in front of whatever value survives, so the pattern still matches), but a 400-character token would eat the whole budget and push the actionable part of the message off the end.

Still: no file is written, and the channels are owner-facing. Surfacing the reason at all is a deliberate trade — a refusal that cannot say why is the failure this whole mechanism exists to remove.

## Registering a service

`services.custom` in `~/.agentwire/config.yaml`:

```yaml
services:
  custom:
    - name: work-tracker             # tmux session name (required)
      project: ~/projects/tracker    # project dir (default: dev source dir)
      posture: bypass                # optional posture override
      roles: tracker                 # optional roles override (comma-separated)
      autostart: true                # boot on portal launch / `agentwire up` (default)
      restart: on-failure            # never | on-failure | always (default on-failure)
      healthcheck:
        kind: tmux_session           # tmux_session (default) | http | command
        interval: 60                 # seconds between watchdog checks
    - name: some-bridge              # a COMMAND service — a plain process
      command: some-bridge --port 9999
      project: ~/projects/bridge     # working directory (default: $HOME)
      autostart: false
    - "simple-service"               # string shorthand: name only, all defaults
```

Setting `roles`, `posture` or `context_policy` alongside `command:` prints a warning and drops them. They are not silently ignored on purpose — a field that reads as a guard while nothing consumes it is worse than no field at all.

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

`agentwire doctor` includes a registry-driven services section: `[ok]` healthy, `[!!]` should-be-running-but-isn't (with the fix command), `[..]` downed or autostart-off. Every line names the service's **kind**, because "session not found" means a dead agent for one and a dead process for the other, and the fix differs. A broken healthcheck on one entry is reported and the loop carries on — one bad service must not abandon the rest of the report.

## Where a command service's output goes — and where it must not

Nowhere on disk. tmux is the supervisor, and that choice **is** the secret-handling answer: stdout and stderr land in the pane's scrollback, which lives in the tmux server's memory behind the per-user socket dir `/tmp/tmux-<uid>` (mode 0700). agentwire adds no redirection — no `>`, no `tee`, no `pipe-pane`. A wrapper that is careful in its own code and then tees stdout into a log has not solved the problem, and the standard #887 holds `~/.agentwire`, `.env` and `portal.token` to applies here too: owner-only or not at all.

One thing that is **not** hidden, and cannot be: `command` itself lands in the process table, which every local user can read. Secrets belong in `~/.agentwire/.env`, read from the environment by the process — never in a service's argv. `agentwire doctor` flags a `command` that looks like it carries one (`--token=`, `--api-key=`, `password=`, `Bearer …`) and names the pattern it matched.

## Restart semantics

The watchdog kills and respawns; it does not resume. A command service is therefore responsible for landing in a sane state on a cold start, and the useful question to ask of any candidate is *what does a restart mid-operation leave behind?* — if the answer is "nothing, the state is per-run and in memory", that is worth a test rather than an assumption, because it is one refactor away from being false and nothing fails when it becomes false.

## Internals

Single source of truth: `agentwire/services.py` — registry synthesis (built-ins + config), healthcheck runners, start/stop, disabled-state file, and the pure `WatchdogState` policy class (unit-tested backoff/notify matrix). The CLI commands wrap it; `agentwire up` and the portal's autostart + watchdog call the CLI (`services up --all`, `services status`), never duplicate the logic.

`start_service` / `stop_service` branch on `svc.command`; `service_kind(svc)` is the one place that answers "agent or command", and `command_secret_risk(svc)` is detection-only (it names the pattern; the caller decides). `stop_service` takes the registry **entry**, not a bare name — the kill path branches on `command`, and a name alone would send `/exit` to a process.

Portal Services column: the sidebar fetches `/api/services/custom` and groups those session names under Services automatically.

Deliberately out of scope (for now): per-project services in `.agentwire.yml`, sidebar health badges.
