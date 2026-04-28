---
name: agentwire-pi-zai
description: Pi coding agent integration (Z.AI) — `pi-zai` / `pi-zai-restricted` / `pi-zai-readonly` session types, install (`npm install -g @mariozechner/pi-coding-agent`), config (`pi.binary`, `pi.default_model`, `zai.api_key`), tool translation (Claude CamelCase → pi lowercase), role injection via `--append-system-prompt`, limitations vs Claude Code (no MCP client, no `--disallowedTools`, no `--resume --fork-session`, no hook integration). Use when setting up pi-zai sessions, debugging pi tool execution, or explaining when to pick pi-zai vs claude-*.
---

# pi-zai — Z.AI via Pi Coding Agent

Pi is a minimal terminal coding agent (MIT licensed, [github.com/badlogic/pi-mono](https://github.com/badlogic/pi-mono)) with native Z.AI provider support. Used for Z.AI work so Claude Code stays pure for Anthropic subscription.

Replaces the claudeGLM env-var wrapper approach, which stopped working when Claude Code's OAuth auth began overriding inline `ANTHROPIC_*` env vars.

## Install

```bash
# One-time
npm install -g @mariozechner/pi-coding-agent

# Verify
agentwire doctor    # reports pi version
pi --version
```

## Session Creation

```bash
# Full access (closest to claude-bypass)
agentwire new -s project --type pi-zai -p ~/projects/project

# Override model
agentwire new -s fast --type pi-zai --model glm-4.7-flash

# Persist type to .agentwire.yml (opt-in)
agentwire new -s project --type pi-zai --persist

# Add custom env vars (repeatable, injected via tmux set-environment)
agentwire new -s project --type pi-zai --env OPENAI_KEY=sk-... --env DEBUG=1
```

## Three Variants

| Type | Tools | Use Case |
|------|-------|----------|
| `pi-zai` | read, bash, edit, write | Full access, closest to claude-bypass |
| `pi-zai-restricted` | read, grep, find, bash | Worker panes — commands allowed, no edits |
| `pi-zai-readonly` | read, grep, find | Audit / inspection — pure file inspection |

Unlike Claude Code's permission modes, pi has no permission system to bypass — the variants translate directly to pi's `--tools` whitelist.

## Config

In `~/.agentwire/config.yaml`:

```yaml
zai:
  api_key: "your-zai-api-key"      # or env ZAI_API_KEY
  base_url: "https://api.z.ai/api/anthropic"
  timeout_ms: 3000000

pi:
  default_model: "glm-5.1"   # default. glm-5.1 | glm-5 | glm-4.7 | glm-4.7-flash | ...
  binary: "pi"             # override if not on PATH (e.g., nvm path)
```

## Session Type Separation

| Use Case | Agent | Config |
|----------|-------|--------|
| Human-directed work | `claude` (Anthropic) | `type: claude-bypass` |
| Cost-sensitive / Z.AI subscription | `pi-zai` (Z.AI native) | `type: pi-zai` |
| Worker panes (Z.AI, no edits) | `pi-zai-restricted` | `type: pi-zai-restricted` |
| Audit/inspection (Z.AI, read-only) | `pi-zai-readonly` | `type: pi-zai-readonly` |

## Tool Translation

Role tool names are translated Claude CamelCase → pi lowercase, and filtered to pi's supported set:

| Claude | Pi |
|--------|-----|
| `Read` | `read` |
| `Bash` | `bash` |
| `Edit` | `edit` |
| `Write` | `write` |
| `Grep` | `grep` |
| `Glob` / `LS` | `find` / `ls` |

Unsupported Claude tools (`WebFetch`, `Task`, `TodoWrite`, MCP tools, etc.) are silently filtered. Role tools not in the pi-supported set drop out.

## Role Injection

Same mechanism as Claude Code — merged role instructions write to a temp file and are passed via `--append-system-prompt "$(<file)"`. Variants `pi-zai-restricted` and `pi-zai-readonly` skip role injection since those are curated contexts.

## Limitations vs Claude Code

- **No MCP client** — pi-zai sessions cannot call agentwire MCP tools. Use Claude Code for orchestrator sessions that need MCP.
- **No `--disallowedTools`** — can only whitelist tools. Roles with a `disallowed` list have no effect on pi.
- **No session forking** — `--resume --fork-session` isn't supported. Linear resume works via `--session <file> --continue`. For fork-like behavior, copy the JSONL file manually.
- **No hook system integration** — idle-handler, damage-control, and user-prompt-submit hooks don't fire for pi sessions.
- **Idle detection works** — pi runs under `node`, so existing tmux process detection correctly identifies idle (shell) vs busy (node) states.

## Session Storage

Pi persists every session to `~/.pi/agent/sessions/<cwd-encoded>/<timestamp>_<uuid>.jsonl`. CWD is encoded with `--` separators (e.g., `/Users/dotdev/projects/foo` → `--Users-dotdev-projects-foo--`).

Each JSONL line records one event: model, user message, assistant message with tool calls, tool results. Sessions are recoverable after crashes — extract file writes from `toolCall` events to rebuild generated artifacts.

## Known Gotchas

- **GLM identity hallucination** — GLM models often claim to be Claude (Z.AI trained on Claude outputs). Don't rely on self-reports; verify via the JSONL event stream (`"provider":"zai"`, Z.AI-formatted response IDs).
- **Pi pre-1.0** — v0.66.1 is the tested baseline. Breaking changes possible between minors.
- **Binary location via nvm** — pi installs at `~/.nvm/versions/node/<ver>/bin/pi`. Set `pi.binary` if not on PATH.
- **API key via tmux set-environment** — `ZAI_API_KEY` is injected onto the tmux session with `tmux set-environment -t <session>` at creation time, so it doesn't appear in `ps auxwww` or shell history. Worker panes inherit automatically. If you spawn pi manually outside an agentwire session, you'll need to export `ZAI_API_KEY` yourself.

## See Also

- `docs/wiki/sessions/pi-zai.md` — full user guide, compatibility matrix, troubleshooting
- `docs/missions/pi-harness-overview.md` — 5-phase integration roadmap
- `agentwire-project-config` skill — `.agentwire.yml` session type field
