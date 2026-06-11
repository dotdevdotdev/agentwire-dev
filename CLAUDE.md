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

The `agentwire-mcp-tools` skill has the full reference (sessions, panes, voice, tasks, outbound channels, scheduler, overnight queue, desktop UI, handoffs). Rule of thumb: MCP for agents, CLI for humans/scripts.

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

Per-project config lives in `.agentwire.yml` at the project root — **keep it gitignored** (personal config; a tracked copy makes worktree-dispatched runs silently use the stale committed version). See `agentwire-project-config` skill for fields and task schema. For pi sessions (zai, deepseek, openai, etc.), see the `agentwire-pi` skill.

## Key Patterns

- **agentwire sessions** coordinate via voice, delegate to workers
- **worker panes** spawn within the orchestrator's session (visible dashboard)
- **Pane 0** = orchestrator, **panes 1+** = workers
- **Damage-control hooks** block dangerous ops (`rm -rf`, `git push --force`, etc.)
- **Smart TTS routing** — audio goes to browser if connected, local speakers if not

### Worker Pane Lifecycle

Workers auto-kill after sending idle notification. The idle hook captures output, sends alert to pane 0, then kills the worker. Manual kill if needed: `agentwire kill --pane 1`.

## Hook Installation

One command installs/refreshes everything agentwire-owned:

```bash
agentwire hooks install   # permission hook, idle handler, queue processor, slash commands
agentwire doctor          # verify (flags stale copies, not just missing ones)
```

Installs (symlinks by default, `--copy` to copy):

| File | Target | Purpose |
|------|--------|---------|
| `agentwire-permission.sh` | `~/.claude/hooks/` | Permission dialogs in portal (registered as `PermissionRequest` hook) |
| `idle-handler.sh` | `~/.claude/hooks/` | Worker notifications + scheduled task completion (registered as `Notification` hook) |
| `queue-processor.sh` | `~/.agentwire/` | Sends queued alerts with 15-second gaps to avoid overwhelming orchestrators |

These files are agentwire-owned: `hooks install` replaces any copy that drifts from the packaged source. Re-run it after `agentwire rebuild` to pick up hook changes.

### Diagnosing Issues

```bash
agentwire doctor                        # all components
tail -f /tmp/claude-hook-debug.log      # hook debug
tail -f /tmp/queue-processor-debug.log  # queue processor
```

## Wiki (Knowledge Base)

LLM-maintained knowledge base at `~/.agentwire/wiki/` using the Karpathy LLM Wiki pattern. Research and debugging knowledge compounds across sessions. Use `/wiki ingest`, `/wiki query <question>`, `/wiki lint` skills. **Before researching**: agents check the wiki first. After discovering: agents write it down.

## Handoffs

`/handoff` distills the current conversation into a shareable bundle: `ai-handoff.md` for another LLM and `show-the-story.html` for humans. The agent does the distillation in-context (free); the CLI/MCP renders deterministically. Outputs in `~/.agentwire/artifacts/handoff-<slug>/`. Full reference: [`docs/wiki/communication/handoff.md`](docs/wiki/communication/handoff.md).

## Missions

First-class auto-dispatcher subsystem (sibling to Sessions/Tasks/Workflows): stateless launchd orchestrators tick the GitHub issue board, spawn worker sessions in isolated worktrees for `agent-ready` issues, route PR-review feedback back to the worker, and reap on PR close. CLI under `agentwire mission ...`, 8 MCP `mission_*` tools. Full reference: [`docs/wiki/missions.md`](docs/wiki/missions.md).

## Usage-Limit Recovery

Deterministic (zero-LLM) recovery from the Claude Code usage-limit dialog: a launchd watchdog (`agentwire limits tick`, 60s) plus ensure's completion poll detect the dialog, park the session (option 1: stop and wait), parse the reset time, email the owner via Resend, and nudge the session to continue after reset. Park state under `~/.agentwire/usage-limit/` guards every surface — ensure exits 7 (`usage_limit`), the scheduler skips dispatch, the idle hook and overnight never reap a parked session. CLI under `agentwire limits ...`. Full reference: [`docs/wiki/usage-limit-recovery.md`](docs/wiki/usage-limit-recovery.md).

## Council

Multi-soul orchestrator sitting: `agentwire-council` fans prompts out to lens sessions (`council-brain`, `council-conscience`, …), each replying take/ack/pass through a file inbox under `~/.agentwire/council/prompts/`; the orchestrator collects and synthesizes with attribution. The standard `soul` role self-excludes from any `council-*` session. CLI under `agentwire council ...`, 5 MCP `council_*` tools. Full reference: [`docs/wiki/council.md`](docs/wiki/council.md).

## Reference Skills

Reference detail lives in skills under `.claude/skills/` — invoke as needed:

| Skill | When to use |
|-------|-------------|
| `agentwire-cli` | Running or composing any `agentwire ...` shell command |
| `agentwire-mcp-tools` | Picking the right MCP tool from inside an agent session |
| `agentwire-config` | Editing `~/.agentwire/config.yaml` (TTS, channels, services, etc.) |
| `agentwire-project-config` | Editing `.agentwire.yml`, defining tasks, roles, idle notifications |
| `agentwire-scheduler` | Scheduled task gates/schedule/priority + overnight queue |
| `agentwire-desktop-ui` | Editing portal static files (sidebar, windows, artifacts) |
| `agentwire-pi` | Setting up pi sessions (zai, deepseek, openai, etc.) via pi coding agent |

## Docs

- CLI: `agentwire --help` or `agentwire <cmd> --help`
- **[`docs/wiki/INDEX.md`](docs/wiki/INDEX.md)** — feature reference manual (sessions, communication, scheduling, integrations, deployment, TTS, internals)

## Mission tracking — GitHub Issues only

**Issues are the single source of truth for plans, status, and history.** The project board is the kanban (Proposed → Accepted → Active → Shipped → Stalled → Declined); the issue body holds the plan; comments hold the phase breadcrumbs + verification notes; PRs reference the issue and close it on merge.

Don't write parallel mission specs into the repo. If a plan is long, expand it inside the issue body or as comments. Post-ship reference content (concepts, architecture, troubleshooting) lives in `docs/wiki/`, never as mission files.

Per-mission convention: branch `mission/{slug}`, push after every commit, PR back to `main` and close the issue. The PR description is the breadcrumb — what was built, decisions locked in, schemas, surprises, verification results.

Full conventions (columns, `feature:*` / `area:*` labels, importing historical work): `~/.claude/rules/project-tracking.md`.
