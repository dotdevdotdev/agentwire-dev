# Portal voice assistant (`agentwire-voice`)

One always-on Claude Code session you talk to about the whole agentwire system
("which sessions are busy", "open a quick task for the voice latency issue").
Decision record: #1197 (option 9). Build plan: #1198.

## What it is

A supervised **service of kind agent** named `agentwire-voice` — same lifecycle
as `agentwire-notifications` (watchdog restart, tmux health check), not a worker,
orchestrator or worktree session, and a root (no parent). It runs in the agentwire
source checkout (`dev.source_dir`) so `CLAUDE.md`, the skills and the agentwire MCP
tools load, with the `voice-assistant` role: ack first, act, report done or failed
out loud, spoken confirm before risky actions, never mutate git state in the
shared checkout.

It is classified as infrastructure (`services._BUILTIN_SERVICE_SESSIONS` and the
portal's Services allowlist): listed under Services and attachable
(`tmux attach -t agentwire-voice`), but out of the topology, cohorts, dangling-PR
checks and idle-nag.

**Model and effort:** Haiku 5.5 (`claude-haiku-5-5`) at `medium` effort, `low` as
the speed fallback. These are per-session `claude --model/--effort` launch flags,
recorded in the session's launch record (`~/.agentwire/sessions/agentwire-voice/metadata.json`),
so `agentwire restart` regenerates them. No other session's defaults and no user
settings change. `agentwire new --effort <level>` is the general per-session knob.

## Commands

| Command | What it does |
|---|---|
| `agentwire voice install [--effort low] [--autostart] [--no-start]` | **Human-only.** Writes the fixed `services.custom` entry into `~/.agentwire/config.yaml` and starts it. Refused without a tty, and blocked for agents by a damage-control protected-command rule (same gate as `buddy service install`). |
| `agentwire voice uninstall` | **Human-only.** Stops it and removes the entry. |
| `agentwire voice ask "<text>"` | Starts the service if needed, waits until it is ready, delivers the text. Returns after delivery; the answer is spoken. The one entry point the portal button and Hammerspoon bind to. |
| `agentwire voice status` | Health plus the model/effort the live session launched with. |
| `agentwire voice start` / `stop` | `services up` / `down` for the entry. `stop` kills the session, so the next start is a fresh conversation. |
| `agentwire voice restart` | Relaunch in place, same conversation (`agentwire restart`). |

All take `--json`.

Autostart is opt-in. With it off, `voice ask` creates the session on demand and the
watchdog leaves it alone. The entry sets `resume: true`, so a respawn (watchdog or
`voice start`) resumes the recorded conversation instead of starting fresh.

## Speech guarantee

The role tells the model to `say` an ack and a result, but a prompt is not a
guarantee: in the first live test (Haiku, medium) the model answered correctly and
never spoke. Naming the deferred tool (`mcp__agentwire__say`) in the role fixed that
run (ack at ~8 s, result at ~16 s after send). Slice 2 of #1198 makes it
deterministic with session-scoped `UserPromptSubmit` (instant ack) and `Stop`
(completion or failure) hooks, passed with `claude --settings` so they fire only for
this session.

## Known gaps (tracked in #1198)

- Audio from `say` reaches only browser clients attached to *this session's* socket.
  With no `agentwire-voice` window open it falls back to the local speakers (default
  TTS tier). Slice 3 routes it to the portal.
- `sessions_list` reports no busy/idle state, so "which sessions are busy" needs the
  system digest from slice 4.
