> Living document. Update this, don't create new versions.

# Pi-ZAI Session Types

Run AgentWire sessions backed by [pi coding agent](https://github.com/badlogic/pi-mono) and Z.AI's GLM models instead of Claude Code. Keeps Claude Code pure for Anthropic subscription, uses pi for cost-sensitive Z.AI work.

## When to Use pi-zai vs Other Session Types

| Use Case | Session Type | Why |
|----------|-------------|-----|
| Human-directed work, orchestration, MCP tools needed | `claude-bypass` / `claude-auto` | Full Claude Code ecosystem |
| Cost-sensitive interactive sessions, no MCP needed | **`pi-zai`** | Native Z.AI, minimal overhead |
| Worker panes doing bounded tasks | **`pi-zai-restricted`** | Read + search + bash, no edits |
| Read-only audit / inspection sessions | **`pi-zai-readonly`** | No bash, pure file inspection |
| Anthropic subscription overnight work | `claude-auto` | Classifier safety net |

## Prerequisites

### Install Pi

```bash
npm install -g @mariozechner/pi-coding-agent
```

Verify installation:

```bash
agentwire doctor  # Should show: [ok] pi: /path/to/pi (v0.66.1)
```

### Configure Z.AI

Add Z.AI credentials to `~/.agentwire/config.yaml`:

```yaml
zai:
  api_key: "your-zai-api-key"
  base_url: "https://api.z.ai/api/anthropic"  # Kept for future consumers; pi uses native ZAI provider
  timeout_ms: 3000000
```

Optionally configure pi defaults:

```yaml
pi:
  default_model: "glm-5.1"   # default. glm-5.1 | glm-5 | glm-5-turbo | glm-4.7 | glm-4.7-flash | ...
  binary: "pi"             # Override if pi is installed somewhere other than $PATH
```

## Session Types

### `pi-zai`

Full tool access (read, bash, edit, write). Closest equivalent of `claude-bypass`.

```bash
agentwire new -s myproject --type pi-zai -p ~/projects/myproject
```

Uses the `default_model` from config (default: `glm-5.1`).

### `pi-zai-restricted`

Whitelists `read, grep, find, bash`. Can inspect and run commands but **cannot modify files**. Use for worker panes that investigate without changing things.

```bash
agentwire new -s audit --type pi-zai-restricted -p ~/projects/myproject
```

Role-provided tool whitelists are ignored for this type (curated list takes precedence).

### `pi-zai-readonly`

Whitelists `read, grep, find`. **No bash, no edits.** Pure inspection. Use for sessions that should only look, never touch.

```bash
agentwire new -s inspect --type pi-zai-readonly -p ~/projects/myproject
```

Role instructions are also ignored for this type.

## `.agentwire.yml` Example

```yaml
type: pi-zai
roles:
  - agentwire
  - worker
voice: may
parent: main
```

All existing `.agentwire.yml` fields work identically. Roles inject via `--append-system-prompt` — same mechanism as Claude Code.

## Model Override

Pick a different GLM model per-session:

```bash
agentwire new -s fast --type pi-zai --model glm-4.7-flash -p ~/projects/myproject
```

Available Z.AI models (as of 2026-04-13):

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

Use `pi --list-models zai` to see the current list.

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

Pi has no `--disallowedTools` flag. It can only whitelist. If a role specifies `disallowed_tools`, it's **ignored** for pi-zai sessions. Use `pi-zai-restricted` or `pi-zai-readonly` variants for tool restrictions instead.

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

### Idle Detection

Pi runs as a `node` process in tmux. AgentWire's idle detection (in `completion.py`) recognizes any non-shell process as an active agent, so pi is detected correctly without changes.

When pi exits (triple Ctrl+C), the pane falls back to the shell (`zsh`/`bash`), and idle detection fires normally.

### What Pi Doesn't Do

- **No MCP client.** Pi sessions cannot call agentwire's MCP tools (`sessions_list`, `pane_spawn`, `say`, etc.). Use Claude Code for orchestrator sessions that need MCP. Workflow nodes (Phase 2) will call agentwire CLI via bash instead.
- **No session forking.** Pi has `--session <file> --continue` for resuming, but no equivalent to Claude Code's `--resume <id> --fork-session`. If you need forking, use Claude Code. Pi sessions can still save + resume linearly.
- **No hook directory.** Pi uses an extension system (`~/.pi/agent/extensions/`) rather than the hooks pattern. AgentWire's idle-handler and damage-control hooks don't integrate with pi. Workers that need those must use Claude Code session types.

## Troubleshooting

### "pi: command not found"

Pi isn't installed or isn't in `$PATH`. Install it:

```bash
npm install -g @mariozechner/pi-coding-agent
```

Or override the binary path in config:

```yaml
pi:
  binary: "/full/path/to/pi"
```

### "No models available. Set API keys in environment variables."

`ZAI_API_KEY` isn't set on the tmux session. Check `~/.agentwire/config.yaml` has the `zai.api_key` field filled in. AgentWire injects the key via `tmux set-environment -t <session>` at creation time, so it's available to pi as an env var but doesn't appear in `ps auxwww` or shell history.

If you spawn pi manually (outside of `agentwire new`), you'll need to `export ZAI_API_KEY=...` yourself first.

### Pi shows changelog on startup

Normal on first launch after version upgrade. Pi displays its changelog once per new version. Safe to ignore — the session is ready to take input.

### Role instructions not taking effect

Roles with only `disallowed_tools` (no `tools` or `instructions`) will appear to do nothing for pi-zai sessions — pi doesn't support disallowed tools. Check you have `instructions:` or `tools:` set in your role.

### Session forks without context

Pi's forking is manual (copy JSONL, run with `--session <copy> --continue`). AgentWire's `fork` command is optimized for Claude Code's `--resume --fork-session` flow and doesn't currently fork pi sessions with inherited context. Start a fresh pi session instead, or re-enter context manually.

## Compatibility Matrix

| Feature | claude-* | pi-zai | pi-zai-restricted | pi-zai-readonly |
|---------|----------|--------|-------------------|-----------------|
| Model backend | Anthropic | Z.AI (native) | Z.AI (native) | Z.AI (native) |
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

- Mission plan: `docs/missions/pi-session-type.md` (this phase)
- Overall roadmap: `docs/missions/pi-harness-overview.md`
- Technology evaluation: `~/.agentwire/wiki/wiki/research/pi-coding-agent-zai-harness.md`
- Pi upstream: https://github.com/badlogic/pi-mono
- Z.AI docs: https://docs.z.ai/
