# AgentWire Wiki

Reference manual for AgentWire features and internals.

> **Living wiki.** Update existing pages, don't create new versions. New work is tracked in [GitHub issues](https://github.com/dotdevdotdev/agentwire-dev/issues) and pull requests, not in this repo.

## Getting Started

New to AgentWire? Start here:

1. **[README](../../README.md)** — what AgentWire is, install, quick start
2. **[Concepts](concepts.md)** — narrative mental model: why tmux, sessions, orchestrator/worker, channels, scheduled work
3. **[Architecture](architecture.md)** — single-page diagram of how the pieces fit together
4. **[Glossary](glossary.md)** — definitions for session, pane, channel, gate, and the rest
5. **[CLAUDE.md](../../CLAUDE.md)** — agent-facing project guide
6. **[Sessions: claude-code-auto-mode](sessions/claude-code-auto-mode.md)** — the safest default for autonomous work
7. **[REPL walkthrough](sessions/repl-tui.md)** — the Textual TUI for interactive sessions

## Sessions

How AgentWire runs AI agents — session types, REPLs, and permission models.

- **[claude-code-auto-mode](sessions/claude-code-auto-mode.md)** — Auto mode session type with classifier safety net
- **[pi](sessions/pi.md)** — Pi coding agent (multi-provider: zai, deepseek, openai, openrouter, …)
- **[REPL TUI](sessions/repl-tui.md)** — Textual REPL walkthrough — slash commands, shortcuts, theming, `--view fanout`

## Communication

How sessions talk to humans and external platforms.

- **[Channels](communication/channels.md)** — email, SMS, Discord, Slack, webhooks → sessions
- **[Hammerspoon push-to-talk](communication/hammerspoon.md)** — global voice hotkeys on macOS

## Scheduling & Workflows

Headless and scheduled execution.

- **[Scheduled workloads](scheduling/scheduled-workloads.md)** — `agentwire ensure`, `.agentwire.yml` task schema, overnight queue
- **[Pi workflows](scheduling/workflows.md)** — YAML-defined DAGs of pi invocations

## Integrations

External tools wired into AgentWire.

- **[Google Workspace CLI (`gws`)](integrations/gws-google-workspace-cli.md)** — Gmail/Drive/Calendar via `@googleworkspace/cli`

## Deployment

Running AgentWire across machines and exposing the portal.

- **[Remote machines](deployment/remote-machines.md)** — SSH-based multi-machine orchestration, WSL2 setup
- **[Remote access](deployment/remote-access.md)** — Cloudflare Tunnel + Zero Trust auth for the portal

## TTS

Voice output backends.

- **[Self-hosted TTS](tts/tts-self-hosted.md)** — local backends (Kokoro, XTTS, etc.)
- **[RunPod TTS](tts/runpod-tts.md)** — serverless GPU TTS

## Internals

Implementation reference for contributors and advanced users.

- **[Portal](internals/portal.md)** — modes, REST API, WebSocket events
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
| `agentwire-scheduler` | Scheduled tasks, gates, overnight queue |
| `agentwire-desktop-ui` | Editing portal static files |
| `agentwire-pi` | Pi sessions for any provider (zai, deepseek, openai, …) |
| `agentwire-workflows` | Authoring/debugging pi workflow YAMLs |

## Mission Archive

Historical design and shipping records: [`docs/missions/completed/`](../missions/completed/). New missions are tracked in [GitHub issues](https://github.com/dotdevdotdev/agentwire-dev/issues) — issue body for the plan, comments for progress, PR description for the canonical end-of-project summary.
