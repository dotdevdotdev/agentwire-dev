---
name: agentwire-pi
description: Pi coding agent (multi-provider) integration — `pi-<provider>` / `pi-<provider>-restricted` / `pi-<provider>-readonly` session types for any pi provider (zai, deepseek, openai, openrouter, etc.). Install (`npm install -g @mariozechner/pi-coding-agent`), config (`pi.binary`, `pi.system_prompt`, `pi.extra_env`, `pi.providers.<name>.{env_var,api_key,default_model}`), custom providers via `~/.pi/agent/models.json`, tool translation (Claude CamelCase → pi lowercase), system-prompt injection (global + role merged), built-in helpers (`agentwire brave`, `agentwire fetch`), limitations vs Claude Code (no MCP client, no `--disallowedTools`, no `--resume --fork-session`, no hook integration). Use when setting up pi sessions for any provider, debugging pi tool execution, or explaining when to pick pi-* vs claude-*.
---

# pi — Pi Coding Agent (multi-provider)

Pi is a minimal terminal coding agent (MIT licensed, [github.com/badlogic/pi-mono](https://github.com/badlogic/pi-mono)) that supports many model providers via `--provider <name>`. Used for non-Anthropic models so Claude Code stays pure for Anthropic subscription auth.

Session types follow `pi-<provider>[-restricted|-readonly]`. The provider, env var, and default model are resolved from `pi.providers.<name>` in `~/.agentwire/config.yaml`. New providers can be added by config alone — no code changes.

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
# Z.AI (closest to claude-bypass cost-wise)
agentwire new -s project --type pi-zai -p ~/projects/project

# DeepSeek
agentwire new -s project --type pi-deepseek -p ~/projects/project

# OpenAI restricted (read+grep+find+bash, no edits)
agentwire new -s audit --type pi-openai-restricted -p ~/projects/audit

# Override model for any provider
agentwire new -s fast --type pi-zai --model glm-4.7-flash

# Persist type to .agentwire.yml (opt-in)
agentwire new -s project --type pi-deepseek --persist

# Add custom env vars (repeatable, injected via tmux set-environment)
agentwire new -s project --type pi-zai --env DEBUG=1
```

If `pi.providers.<provider>` is missing from config, session creation fails fast with a clear error:

```
ValueError: No config for pi provider 'foo'. Add pi.providers.foo to ~/.agentwire/config.yaml
```

## Variants

| Suffix | Tools | Use Case |
|--------|-------|----------|
| (none) | read, bash, edit, write | Full access, closest to claude-bypass |
| `-restricted` | read, grep, find, bash | Worker panes — commands allowed, no edits |
| `-readonly` | read, grep, find | Audit / inspection — pure file inspection |

Pi has no permission system to bypass — variants translate directly to pi's `--tools` whitelist. Restricted/readonly variants are curated contexts: `pi.system_prompt` and role instructions are **skipped**.

## Config

In `~/.agentwire/config.yaml`:

```yaml
pi:
  binary: "pi"  # path override if not on PATH (e.g., nvm-installed)

  # Appended via --append-system-prompt to every non-restricted pi-* session.
  # Use to teach pi about local helpers — agentwire brave, agentwire fetch, etc.
  system_prompt: |
    ## Web Search
    Use `agentwire brave "<query>"` for web search via the Brave Search API.

    ## Fetching URLs
    Use `agentwire fetch <url>` to fetch a page as clean markdown.

  # Env vars injected into every pi-* session (in addition to the provider key)
  extra_env:
    BRAVE_SEARCH_API_KEY: "BSA..."

  providers:
    zai:
      env_var: ZAI_API_KEY
      api_key: "..."
      default_model: glm-5.1
    deepseek:
      env_var: DEEPSEEK_API_KEY
      api_key: "..."
      default_model: deepseek-chat
    # add more as needed: openai, openrouter, anthropic, ...
```

Provider key flows via `tmux set-environment` so it never appears in `ps auxwww` or shell history.

## Custom Providers via `~/.pi/agent/models.json`

For providers pi doesn't ship with built-in (e.g. DeepSeek), register them as OpenAI-compatible endpoints:

```json
{
  "providers": {
    "deepseek": {
      "baseUrl": "https://api.deepseek.com/v1",
      "api": "openai-completions",
      "apiKey": "sk-...",
      "models": [
        { "id": "deepseek-chat" },
        { "id": "deepseek-reasoner", "reasoning": true }
      ]
    }
  }
}
```

Then in `~/.agentwire/config.yaml`:

```yaml
pi:
  providers:
    deepseek:
      env_var: DEEPSEEK_API_KEY
      api_key: "sk-..."
      default_model: deepseek-chat
```

Same pattern works for OpenRouter, Together AI, Groq, or any other OpenAI-compatible API.

## Built-in Helpers in pi Sessions

Two CLI helpers are automatically taught to pi via `pi.system_prompt`:

| Helper | Purpose |
|--------|---------|
| `agentwire brave "<query>"` | Brave Search wrapper (output: `title \| url \| age \| description`, one per line) |
| `agentwire fetch <url>` | Fetches a URL via Jina Reader — handles JS-rendered pages, returns clean markdown |

These work in any pi session because `pi.extra_env.BRAVE_SEARCH_API_KEY` and the system-prompt instructions are injected globally. Add new helpers by extending `pi.system_prompt`.

## System Prompt Composition

For non-restricted sessions, the temp file passed to `--append-system-prompt` is:

```
{pi.system_prompt}

{role.instructions if any}
```

Order matters: global comes first so role-specific instructions can build on it. Empty pieces are dropped — if neither is set, no `--append-system-prompt` flag is added.

## Tool Translation

Role tool names are translated Claude CamelCase → pi lowercase, then filtered to pi's supported set:

| Claude | Pi |
|--------|-----|
| `Read` | `read` |
| `Bash` | `bash` |
| `Edit` | `edit` |
| `Write` | `write` |
| `Grep` | `grep` |
| `Glob` / `LS` | `find` / `ls` |

Unsupported Claude tools (`WebFetch`, `Task`, `TodoWrite`, MCP tools, etc.) are silently filtered. Roles with a `disallowed` list have no effect on pi (only whitelist supported).

## Limitations vs Claude Code

- **No MCP client** — pi sessions cannot call agentwire MCP tools. Use Claude Code for orchestrator sessions that need MCP.
- **No `--disallowedTools`** — can only whitelist tools.
- **No session forking** — `--resume --fork-session` isn't supported. Linear resume works via `--session <file> --continue`. Copy the JSONL manually for fork-like behavior.
- **No hook system integration** — idle-handler, damage-control, and user-prompt-submit hooks don't fire for pi sessions.
- **Idle detection works** — pi runs under `node`, so existing tmux process detection correctly identifies idle (shell) vs busy (node) states.

## Session Storage

Pi persists every session to `~/.pi/agent/sessions/<cwd-encoded>/<timestamp>_<uuid>.jsonl`. CWD is encoded with `--` separators (e.g., `/Users/dotdev/projects/foo` → `--Users-dotdev-projects-foo--`).

Each JSONL line records one event: model, user message, assistant message with tool calls, tool results. Sessions are recoverable after crashes — extract file writes from `toolCall` events to rebuild generated artifacts.

## Known Gotchas

- **GLM identity hallucination** — GLM models often claim to be Claude (Z.AI trained on Claude outputs). Don't rely on self-reports; verify via the JSONL event stream (`"provider":"zai"`, Z.AI-formatted response IDs).
- **Pi pre-1.0** — v0.66.1 was the original baseline. Breaking changes possible between minors.
- **Binary location via nvm** — pi installs at `~/.nvm/versions/node/<ver>/bin/pi`. Set `pi.binary` if not on PATH.
- **Provider key via tmux set-environment** — keys are injected onto the tmux session with `tmux set-environment -t <session>` at creation time, so they don't appear in `ps auxwww`. Worker panes inherit automatically. If you spawn pi manually outside an agentwire session, you'll need to export the env var yourself.

## Session Type Selection

| Use Case | Type | Notes |
|----------|------|-------|
| Human-directed work, full Anthropic | `claude-bypass` | MCP-aware, hooks fire |
| Cost-sensitive Z.AI | `pi-zai` | Z.AI subscription / pay-as-you-go |
| Cost-sensitive DeepSeek | `pi-deepseek` | Cheap, strong on code |
| Anything OpenAI-compatible | `pi-<provider>` | Add to `pi.providers` + optionally `~/.pi/agent/models.json` |
| Worker pane (no edits) | `pi-<provider>-restricted` | read+grep+find+bash whitelist |
| Audit/inspection (read-only) | `pi-<provider>-readonly` | read+grep+find whitelist |

## See Also

- `docs/wiki/sessions/pi.md` — full user guide, compatibility matrix, troubleshooting
- `docs/missions/pi-harness-overview.md` — pi integration roadmap
- `agentwire-config` skill — `pi:` config block reference
- `agentwire-project-config` skill — `.agentwire.yml` session type field
