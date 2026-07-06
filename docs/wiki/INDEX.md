# AgentWire Wiki

Reference manual for AgentWire features and internals.

> **Living wiki.** Update existing pages, don't create new versions. New work is tracked in [GitHub issues](https://github.com/dotdevdotdev/agentwire-dev/issues) and pull requests, not in this repo.

## Getting Started

New to AgentWire? Start here:

1. **[Quickstart](quickstart.md)** — install → first session → voice → first scheduled task → first channel, in 5 minutes
2. **[Concepts](concepts.md)** — narrative mental model: why tmux, sessions, orchestrator/worker, channels, scheduled work
3. **[Architecture](architecture.md)** — single-page diagram of how the pieces fit together
4. **[Glossary](glossary.md)** — definitions for session, pane, channel, gate, and the rest
5. **[README](../../README.md)** — what AgentWire is, full install matrix, feature list
6. **[CLAUDE.md](../../CLAUDE.md)** — agent-facing project guide
7. **[Sessions: claude-code-auto-mode](sessions/claude-code-auto-mode.md)** — the safest default for autonomous work

## Sessions

How AgentWire runs AI agents — session types, REPLs, and permission models.

- **[Worktree sessions](sessions/worktree-sessions.md)** — `agentwire worktree <name>`: isolated branch + worktree + standalone session for one unit of work; repo-derived base branch, naming templates, monorepo support, local branch↔session registry (`--list`/`--remove`/`--prune`/`--dangling`); `--kind orchestrator` / `agentwire orchestrator` for a durable project window on the same topology (role⟂topology, #716)
- **[claude-code-auto-mode](sessions/claude-code-auto-mode.md)** — Auto mode session type with classifier safety net
- **[pi](sessions/pi.md)** — Pi coding agent (multi-provider: zai, deepseek, openai, openrouter, …)
- **[Window sizing](sessions/window-sizing.md)** — how tmux `window-size` policies interact with the portal (v1.33+ behavior change, healing stuck windows, policy picker)
- **[Custom services](services.md)** — registered long-running sessions: autostart on portal launch, watchdog health checks + restart with backoff, `agentwire services` CLI
- **[Council](council.md)** — multi-soul orchestrator sitting: fan a prompt out to lens sessions (brain, conscience, gut, critic, …), collect via file inbox, synthesize with attribution
- **[Briefing Mode](briefing-mode.md)** — asymmetric-verbosity orchestration: a terse `anchor` fans out verbose `correspondent` worktrees that signal passively (`msg ingest`), then briefs the human across voice + screen (`say(text=, display=)`) on cue. Roles + dropbox + passive-pull + worktree lifecycle quartet
- **[Prompt routing](sessions/prompt-routing.md)** — permission/plan/AskUserQuestion prompts in a child session route to its parent (hook path + watchdog sweep); guarded `agentwire prompts answer`, no auto-answering
- **[Polite messaging](sessions/messaging.md)** — `agentwire msg` drops typed messages into a per-session file inbox and injects them only when the input box is empty (`prompt_is_empty`) and the pane is safe; never clobbers a human draft, the way `agentwire send` does. `@all` broadcast, MCP `msg_send`/`msg_inbox`. Plus the **passive `ingest`** kind (never auto-delivered; pulled with `msg pull`) + typed `ref` pointer — the awareness primitive behind Briefing Mode

## Communication

How sessions talk to humans and external platforms.

- **[Channels](communication/channels.md)** — outbound notifications (email, SMS) from sessions
- **[Hammerspoon push-to-talk](communication/hammerspoon.md)** — global voice hotkeys on macOS
- **[Conversation handoffs](communication/handoff.md)** — `/handoff` produces a portable bundle (LLM-targeted .md + human-targeted .html) for async teammate pickup

## Scheduling

Headless and scheduled execution.

- **[Scheduled workloads](scheduling/scheduled-workloads.md)** — `agentwire ensure`, `.agentwire.yml` task schema
- **[Usage-limit recovery](usage-limit-recovery.md)** — deterministic detect → park → email → auto-resume for the Claude Code usage-limit dialog; launchd watchdog, zero LLM involvement

## Security

- **[Secrets & API keys](security/secrets.md)** — `~/.agentwire/.env` is the one place every key lives; which vars each feature reads; the `api_key_env` pattern for new integrations
- **[Remote-access hardening](security/remote-access-hardening.md)** — threat model for the any-device→tunnel→portal→shell path; network footprint map (what agentwire owns vs BYO tunnels); the portal auth boundary and the per-device / capability-scope / freeze-config hardening plan (#396, #420, #423–#425)
- **[Damage control](internals/damage-control.md)** — safety hooks: rules, patterns, audit log
- **[Damage-control matcher hardening](security/damage-control-hardening.md)** — consolidated note for the matcher hardening shipped in PR #500
- **[pip-audit](security/pip-audit.md)** — dependency CVE triage workflow

## Integrations

External tools wired into AgentWire.

- **[Google Workspace CLI (`gws`)](integrations/gws-google-workspace-cli.md)** — Gmail/Drive/Calendar via `@googleworkspace/cli`

## Deployment

Running AgentWire across machines and exposing the portal.

- **[Remote machines](deployment/remote-machines.md)** — SSH-based multi-machine orchestration, WSL2 setup
- **[Remote access](deployment/remote-access.md)** — Cloudflare Tunnel + Zero Trust auth for the portal

## Research

Recon notes and evaluations that inform direction (not yet implemented).

- **[Orchestration transport alternatives](research/orchestration-transport-alternatives.md)** — the "we don't use SSH" competitor pitch, evaluated: claims-vs-reality on faster/more-secure, and why the cheap wins are SSH `ControlMaster` multiplexing + a Tailscale mesh underlay rather than a new transport ([#297](https://github.com/dotdevdotdev/agentwire-dev/issues/297))
- **[Briefing Mode feasibility](research/briefing-mode-feasibility.md)** — asymmetric-verbosity orchestration evaluation that informed the shipped Briefing Mode

## Voice (TTS & STT)

Tiered model: `default` (in-process Kokoro-82M, zero setup — what a fresh
install gets; browser speechSynthesis covers the one-time model download),
`cloud` (STT only — any OpenAI-compatible transcription API, key from env),
and `custom` (any model behind a small HTTP shim).

- **[Shim contract](voice/shim-contract.md)** — the tiers, the envelope (instructions/options pass-through), capabilities + tool_prompt injection, a from-scratch shim example
- **[Self-hosted TTS](voice/tts-self-hosted.md)** — the bundled reference shim's engines (Kokoro, Chatterbox, Zonos)
- **[Cloud STT](voice/stt-cloud.md)** — `stt.backend: cloud`, portal → any OpenAI-compatible transcription API, no shim daemon
- **[Self-hosted STT](voice/stt-self-hosted.md)** — moonshine / faster-whisper reference shim, push-to-talk latency knobs

## Internals

Implementation reference for contributors and advanced users.

- **[Portal](internals/portal.md)** — modes, REST API, WebSocket events
- **[Large parallel refactors](internals/parallel-refactor.md)** — splitting a huge file across parallel worktrees: positional-interleaving conflicts, regenerate-against-fresh-base + sequential merges, foundation-first, verification discipline
- **[Window collage](internals/window-collage.md)** — Mission Control overlay: preview-tile architecture + why mutating real WinBox windows can never work
- **[Shell escaping](internals/shell-escaping.md)** — how complex strings cross tmux boundaries
- **[Damage control](internals/damage-control.md)** — safety hooks: rules, patterns, audit log
- **[Troubleshooting](internals/troubleshooting.md)** — common issues and fixes

## Skills

Agent-facing reference lives in `.claude/skills/` and loads automatically inside Claude Code:

| Skill | Topic |
|---|---|
| `agentwire-cli` | Composing `agentwire ...` shell commands |
| `agentwire-mcp-tools` | Picking the right MCP tool inside a session |
| `agentwire-config` | Editing `~/.agentwire/config.yaml` |
| `agentwire-project-config` | Editing `.agentwire.yml`, defining tasks/roles |
| `agentwire-scheduler` | Scheduled tasks, gates |
| `agentwire-desktop-ui` | Editing portal static files |
| `agentwire-pi` | Pi sessions for any provider (zai, deepseek, openai, …) |

## Issue tracking

Plans, status, and history live in [GitHub issues](https://github.com/dotdevdotdev/agentwire-dev/issues). Issue body = plan, comments = progress breadcrumbs, PR body = end-of-task summary. (This is a contributor convention, not something agentwire ships.)
