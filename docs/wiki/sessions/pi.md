> Living document. Update this, don't create new versions.

# Pi Session Types (multi-provider)

Run AgentWire sessions backed by [pi coding agent](https://github.com/badlogic/pi-mono) using any model provider — Z.AI, DeepSeek, OpenAI, OpenRouter, etc. Keeps Claude Code pure for Anthropic subscription auth; uses pi for cost-sensitive non-Anthropic work.

Session types follow `pi-<provider>[-restricted|-readonly]`. Adding a new provider is config-only — no code changes.

## When to Use pi-* vs Other Session Types

| Use Case | Session Type | Why |
|----------|-------------|-----|
| Human-directed work, orchestration, MCP tools needed | `claude-bypass` / `claude-auto` | Full Claude Code ecosystem |
| Cost-sensitive interactive sessions on Z.AI | **`pi-zai`** | Native Z.AI GLM, minimal overhead |
| Cost-sensitive interactive sessions on DeepSeek | **`pi-deepseek`** | Strong code performance, very cheap |
| Anything OpenAI-compatible | **`pi-<provider>`** | Add to `pi.providers` (+ optionally `~/.pi/agent/models.json`) |
| Worker panes doing bounded tasks | **`pi-<provider>-restricted`** | Read + search + bash, no edits |
| Read-only audit / inspection sessions | **`pi-<provider>-readonly`** | No bash, pure file inspection |
| Anthropic subscription overnight work | `claude-auto` | Classifier safety net |

## Prerequisites

### Install Pi

```bash
npm install -g @mariozechner/pi-coding-agent
```

Verify installation:

```bash
agentwire doctor  # Should show: [ok] pi: /path/to/pi (vX.Y.Z)
```

### Configure Providers

Add a `pi:` block to `~/.agentwire/config.yaml`:

```yaml
pi:
  binary: "pi"  # path override if pi isn't on PATH (e.g., nvm-installed)

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

If `pi.providers.<provider>` is missing for the requested type, session creation fails fast with a clear error.

## Adding a New Provider

For providers pi ships with built-in (zai, openai, openrouter, anthropic, …): add a `pi.providers.<name>` entry and you're done.

For providers pi doesn't know about (e.g. DeepSeek), register them in `~/.pi/agent/models.json` as OpenAI-compatible:

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

Then add the matching `pi.providers.deepseek` entry. Same pattern for OpenRouter, Together AI, Groq, etc.

## Session Types

### `pi-<provider>`

Full tool access (read, bash, edit, write). Closest equivalent of `claude-bypass`.

```bash
agentwire new -s myproject --type pi-zai -p ~/projects/myproject
agentwire new -s myproject --type pi-deepseek -p ~/projects/myproject
```

Uses `default_model` from `pi.providers.<provider>.default_model`.

### `pi-<provider>-restricted`

Whitelists `read, grep, find, bash`. Can inspect and run commands but **cannot modify files**. Use for worker panes that investigate without changing things.

```bash
agentwire new -s audit --type pi-zai-restricted -p ~/projects/myproject
```

Role-provided tool whitelists, role instructions, and `pi.system_prompt` are all skipped — the curated context wins.

### `pi-<provider>-readonly`

Whitelists `read, grep, find`. **No bash, no edits.** Pure inspection.

```bash
agentwire new -s inspect --type pi-zai-readonly -p ~/projects/myproject
```

Same skip semantics as `-restricted`.

## `.agentwire.yml` Example

```yaml
type: pi-deepseek
roles:
  - agentwire
  - worker
voice: may
parent: main
```

All existing `.agentwire.yml` fields work identically. Roles inject via `--append-system-prompt` — same mechanism as Claude Code.

## Built-in Helpers

Two CLI helpers are auto-injected into every non-restricted pi session via `pi.system_prompt`:

| Helper | Purpose |
|--------|---------|
| `agentwire brave "<query>"` | Brave Search wrapper |
| `agentwire fetch <url>` | Fetches a URL via Jina Reader (handles JS-rendered pages) |

Both work because `pi.extra_env.BRAVE_SEARCH_API_KEY` is injected at session creation. Add new helpers by extending `pi.system_prompt`.

## Model Override

Pick a different model per-session:

```bash
agentwire new -s fast --type pi-zai --model glm-4.7-flash -p ~/projects/myproject
agentwire new -s reason --type pi-deepseek --model deepseek-reasoner -p ~/projects/myproject
```

Use `pi --list-models <provider>` to see currently available models. For example, on Z.AI as of 2026-04-13:

| Model | Context | Max Output | Thinking | Vision |
|-------|---------|-----------|----------|--------|
| glm-5.1 | 200K | 131K | yes | no |
| glm-5 | 205K | 131K | yes | no |
| glm-5-turbo | 200K | 131K | yes | no |
| glm-5v-turbo | 200K | 131K | yes | yes |
| glm-4.7 | 205K | 131K | yes | no |
| glm-4.7-flash | 200K | 131K | yes | no |
| glm-4.6 | 205K | 131K | yes | no |
| glm-4.5 | 131K | 98K | yes | no |

## Roles and Tools

Pi supports fewer tools than Claude Code. Role tool lists are **translated** to pi's names:

| Claude Code Tool | Pi Tool |
|------------------|---------|
| Read | read |
| Bash | bash |
| Edit | edit |
| Write | write |
| Grep | grep |
| Glob | find |
| LS | ls |
| Task, WebFetch, WebSearch, MCP tools, etc. | **not supported** |

Unsupported tools in a role's tool list are silently filtered out. If your role depends on a tool pi doesn't have, the session will still start but that tool won't be available.

### Disallowed Tools

Pi has no `--disallowedTools` flag. It can only whitelist. If a role specifies `disallowed_tools`, it's **ignored** for pi sessions. Use `pi-<provider>-restricted` or `pi-<provider>-readonly` variants for tool restrictions instead.

## System Prompt Composition

For non-restricted sessions, the temp file passed to `--append-system-prompt` is:

```
{pi.system_prompt}

{role.instructions if any}
```

Order matters — global comes first so role-specific instructions can build on or override it. Empty pieces are dropped.

## Architecture Notes

### CLI Flag Mapping

| Feature | Claude Code | Pi |
|---------|------------|-----|
| System prompt injection | `--append-system-prompt "text"` | `--append-system-prompt "text"` |
| Tool whitelist | `--tools Bash,Read` | `--tools bash,read` |
| Tool blacklist | `--disallowedTools X` | Not supported |
| Model override | `--model haiku` | `--model glm-5.1` |
| Skip permissions | `--dangerously-skip-permissions` | Not needed (no permission system) |
| JSON output | Not available | `--mode json` |
| Non-interactive | Via tmux | `-p "prompt"` |

### Provider Key Injection

Provider keys flow via `tmux set-environment` so they never appear in `ps auxwww` or shell history. Each provider entry's `env_var` is set on the session at creation time. Worker panes inherit automatically.

If you spawn pi manually (outside of `agentwire new`), you'll need to `export <ENV_VAR>=...` yourself first.

### Idle Detection

Pi runs as a `node` process in tmux. AgentWire's idle detection (in `completion.py`) recognizes any non-shell process as an active agent, so pi is detected correctly without changes. When pi exits (triple Ctrl+C), the pane falls back to the shell, and idle detection fires normally.

### What Pi Doesn't Do

- **No MCP client.** Pi sessions cannot call agentwire's MCP tools (`sessions_list`, `pane_spawn`, `say`, etc.). Use Claude Code for orchestrator sessions that need MCP. Workflow nodes call agentwire CLI via bash instead.
- **No session forking.** Pi has `--session <file> --continue` for linear resume, but no equivalent to Claude Code's `--resume <id> --fork-session`. Manual fork = copy the JSONL.
- **No hook directory.** Pi uses an extension system (`~/.pi/agent/extensions/`) rather than the hooks pattern. AgentWire's idle-handler and damage-control hooks don't integrate with pi. Workers that need those must use Claude Code session types.

## Troubleshooting

### "pi: command not found"

Pi isn't installed or isn't in `$PATH`. Install:

```bash
npm install -g @mariozechner/pi-coding-agent
```

Or override the binary path:

```yaml
pi:
  binary: "/full/path/to/pi"
```

### `ValueError: No config for pi provider 'X'`

You requested `pi-X` but `pi.providers.X` isn't defined in `~/.agentwire/config.yaml`. Add the entry with at least `env_var`, `api_key`, and `default_model`. For non-built-in providers, also register them in `~/.pi/agent/models.json` (see "Adding a New Provider" above).

### "No models available. Set API keys in environment variables."

The provider's env var (e.g. `ZAI_API_KEY`, `DEEPSEEK_API_KEY`) isn't set on the tmux session. Check `pi.providers.<provider>.api_key` is filled in. AgentWire injects the key via `tmux set-environment -t <session>` at creation time.

### Pi shows changelog on startup

Normal on first launch after version upgrade. Pi displays its changelog once per new version. Safe to ignore.

### Role instructions not taking effect

Roles with only `disallowed_tools` (no `tools` or `instructions`) will appear to do nothing — pi doesn't support disallowed tools. Check the role has `instructions:` or `tools:` set.

### Session forks without context

Pi's forking is manual (copy JSONL, run with `--session <copy> --continue`). AgentWire's `fork` command is optimized for Claude Code's `--resume --fork-session` flow and doesn't currently fork pi sessions with inherited context. Start a fresh pi session, or re-enter context manually.

## Compatibility Matrix

| Feature | claude-* | pi-* | pi-*-restricted | pi-*-readonly |
|---------|----------|------|-----------------|---------------|
| Model backend | Anthropic | configured provider | configured provider | configured provider |
| Interactive (tmux send-keys) | ✓ | ✓ | ✓ | ✓ |
| Role instructions | ✓ | ✓ | curated | curated |
| Role tool whitelist | ✓ | ✓ | fixed | fixed |
| Role tool blacklist | ✓ | ✗ | ✗ | ✗ |
| MCP tools | ✓ | ✗ | ✗ | ✗ |
| Session forking (`--resume --fork-session`) | ✓ | ✗ (manual only) | ✗ | ✗ |
| AgentWire hooks (idle, damage) | ✓ | ✗ | ✗ | ✗ |
| CLAUDE.md / AGENTS.md loading | ✓ | ✓ | ✓ | ✓ |
| Print mode / headless | via tmux | ✓ (`-p`) | ✓ (`-p`) | ✓ (`-p`) |
| JSON event stream | ✗ | ✓ (`--mode json`) | ✓ | ✓ |

## See Also

- `agentwire-pi` skill — concise reference for setting up pi sessions
- `agentwire-config` skill — `pi:` config block field reference
- Mission plan: `docs/missions/pi-session-type.md`
- Overall roadmap: `docs/missions/pi-harness-overview.md`
- Pi upstream: https://github.com/badlogic/pi-mono
- Z.AI docs: https://docs.z.ai/
- DeepSeek docs: https://api-docs.deepseek.com/
