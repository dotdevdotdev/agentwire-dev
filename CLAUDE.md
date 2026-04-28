# AgentWire

Voice interface for AI coding agents. Push-to-talk from any device to tmux sessions running Claude Code.

**No Backwards Compatibility** - Pre-launch, no customers. Change things completely, no legacy fallbacks.

**Hierarchical Delegation** - Before editing files in OTHER projects (e.g., `~/projects/agentwire-website/`), check `agentwire_sessions_list()`. If a session exists for that project, use `agentwire_session_send()` instead of editing directly. See `~/.claude/rules/delegation.md`.

## Dev Workflow

`uv tool install` caches builds and ignores source changes.

```bash
# During development (picks up changes instantly)
agentwire portal start --dev

# After structural changes (pyproject.toml, new files)
agentwire rebuild

# After code changes: ALWAYS do both
agentwire rebuild && agentwire portal restart --dev
```

Rebuild alone = stale static files. Restart alone = stale Python. The MCP server runs as a separate process started by Claude Code — session restart required after rebuild to pick up MCP changes.

## CLI is the Single Source of Truth

All session/machine logic lives in CLI commands (`agentwire/__main__.py`). The portal (`agentwire/server.py`) is a thin wrapper that:

1. Calls CLI via `run_agentwire_cmd(["command", "args"])`
2. Parses JSON output (`--json` flag)
3. Adds WebSocket/real-time features

**When adding new functionality:**
1. Implement in CLI first with `--json` output
2. Portal calls CLI, doesn't duplicate logic
3. Never bypass CLI with direct tmux/subprocess calls

Full CLI command reference lives in the `agentwire-cli` skill.

## MCP Server (For Agents)

**Agents running in agentwire sessions should use MCP tools instead of CLI commands.**

The `agentwire-mcp-tools` skill has the full reference (87 tools covering sessions, panes, voice, tasks, channels, scheduler, overnight queue, desktop UI). Rule of thumb: MCP for agents, CLI for humans/scripts.

**Note:** MCP tools don't support git worktree creation. For isolated commits with worktrees, use the CLI `agentwire spawn --branch <name>` directly.

## Config Layout (`~/.agentwire/`)

| File | Purpose |
|------|---------|
| `config.yaml` | Main config (see `agentwire-config` skill) |
| `machines.json` | Remote machines registry |
| `scripts/` | Machine-specific helper scripts (TTS, startup, service wrappers). Local only, not version controlled. `~/bin/` entries should symlink here. |
| `voices/` | Custom TTS voice samples |
| `uploads/` | Uploaded images for cross-machine sharing |
| `artifacts/` | Agent-generated HTML for artifact windows |
| `wiki/` | LLM-maintained knowledge base (Karpathy LLM Wiki pattern) |
| `logs/` | Audit logs for damage-control |

Per-project config lives in `.agentwire.yml` at the project root — see `agentwire-project-config` skill for fields and task schema. For Z.AI / pi-zai sessions, see the `agentwire-pi-zai` skill.

## Key Patterns

- **agentwire sessions** coordinate via voice, delegate to workers
- **worker panes** spawn within the orchestrator's session (visible dashboard)
- **Pane 0** = orchestrator, **panes 1+** = workers
- **Damage-control hooks** block dangerous ops (`rm -rf`, `git push --force`, etc.)
- **Smart TTS routing** — audio goes to browser if connected, local speakers if not

### Worker Pane Lifecycle

Workers auto-kill after sending idle notification. The idle hook captures output, sends alert to pane 0, then kills the worker. Manual kill if needed: `agentwire kill --pane 1`.

## Hook Installation

Install the idle notification hook:

```bash
mkdir -p ~/.claude/hooks
agentwire hooks install
agentwire doctor  # verify
```

The hook lives at `~/.claude/hooks/idle-handler.sh` and fires on `idle_prompt` notifications.

### Queue Processor

```bash
mkdir -p ~/.agentwire
cp ~/projects/agentwire-dev/scripts/queue-processor.sh ~/.agentwire/
chmod +x ~/.agentwire/queue-processor.sh
```

Sends queued alerts with 15-second gaps to prevent overwhelming orchestrators.

### Diagnosing Issues

```bash
agentwire doctor                        # all components
tail -f /tmp/claude-hook-debug.log      # hook debug
tail -f /tmp/queue-processor-debug.log  # queue processor
```

## Wiki (Knowledge Base)

LLM-maintained knowledge base at `~/.agentwire/wiki/` using the Karpathy LLM Wiki pattern. Research and debugging knowledge compounds across sessions. Use `/wiki ingest`, `/wiki query <question>`, `/wiki lint` skills. **Before researching**: agents check the wiki first. After discovering: agents write it down.

## Reference Skills

Reference detail lives in skills under `.claude/skills/` — invoke as needed:

| Skill | When to use |
|-------|-------------|
| `agentwire-cli` | Running or composing any `agentwire ...` shell command |
| `agentwire-mcp-tools` | Picking the right MCP tool from inside an agent session |
| `agentwire-config` | Editing `~/.agentwire/config.yaml` (TTS, channels, services, etc.) |
| `agentwire-project-config` | Editing `.agentwire.yml`, defining tasks, roles, idle notifications |
| `agentwire-scheduler` | Scheduled task gates/schedule/priority + overnight queue + workflow-backed tasks |
| `agentwire-desktop-ui` | Editing portal static files (sidebar, windows, artifacts) |
| `agentwire-pi-zai` | Setting up Z.AI sessions via pi coding agent |
| `agentwire-workflows` | Authoring or debugging pi workflow YAMLs (`agentwire workflow ...`) |

## Docs

- CLI: `agentwire --help` or `agentwire <cmd> --help`
- **[`docs/wiki/INDEX.md`](docs/wiki/INDEX.md)** — feature reference manual (sessions, communication, scheduling, integrations, deployment, TTS, internals)
- `docs/missions/completed/` — historical design + shipping records (per-mission)
- `docs/missions/pi-harness-overview.md` — pi integration roadmap (active reference)

## Mission tracking

New missions live in [GitHub issues](https://github.com/dotdevdotdev/agentwire-dev/issues). Issue body = plan; comments = progress updates; PR description = canonical end-of-project summary. Don't add new mission docs to this repo — only the wiki receives post-ship reference content.
