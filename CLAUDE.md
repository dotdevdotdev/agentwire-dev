# AgentWire

Voice interface for AI coding agents. Push-to-talk from any device to tmux sessions running Claude Code.

**No Backwards Compatibility** - Pre-launch, no customers. Change things completely, no legacy fallbacks.

## Dev Workflow

`uv tool install` caches builds and ignores source changes.

```bash
# Step 0 — after a remote merge, pull first. rebuild reinstalls whatever is
# checked out, so a never-pulled main silently ships stale code.
git pull --ff-only

# During development (picks up changes instantly)
agentwire portal start --dev

# After structural changes (pyproject.toml, new files)
agentwire rebuild

# After code changes: ALWAYS do both
git pull --ff-only && agentwire rebuild && agentwire portal restart --dev
```

Rebuild alone = stale static files. Restart alone = stale Python. The MCP server runs as a separate process started by Claude Code — session restart required after rebuild to pick up MCP changes. `rebuild` now refuses (warn + `--force` to override) when the checkout is behind `origin/main`, and `agentwire doctor` flags a behind-main checkout, a disabled kill switch (`enabled: false` in `~/.agentwire/damagecontrol.yml`), and damage-control rule/hook/matcher drift.

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

The `agentwire-mcp-tools` skill has the full reference (sessions, panes, voice, tasks, outbound channels, scheduler, desktop UI, handoffs). Rule of thumb: MCP for agents, CLI for humans/scripts.

**Note:** MCP tools don't support git worktree creation. For worktree-isolated work, use the CLI directly — and pick the right primitive:

| Term | Command | What you get |
|------|---------|--------------|
| **Worktree session** | `agentwire worktree <name> -p <repo>` | **Standalone tmux session** named `{project}-<name>`, new branch `<name>` from origin/main, worktree under `~/worktrees/`. Survives independently; report-back via `agentwire notify-parent --to <orchestrator>`. Etiquette (isolation, no rebuild/restart, verify in-worktree, draft PR + notify-back) is intrinsic — the `worktree-session` role is auto-injected by the verb, so first prompts only need the task. |
| **Worker pane** | `agentwire spawn --branch <name>` | A **pane inside the current session** (pane 1+), worktree on `<name>`. Inherits the session's dashboard; idle hook reaps it. |

When the owner says "worktree session", they mean the standalone session (`agentwire worktree`), **never** `spawn` panes. Worker panes are for small subtasks watched by the orchestrator; worktree sessions are for parallel autonomous work. The verb sets the posture: `worktree`/`new` default to the bypass posture (full access), `spawn` to restricted — no `--type` needed for the common case.

## Config Layout (`~/.agentwire/`)

| File | Purpose |
|------|---------|
| `config.yaml` | Main config (see `agentwire-config` skill) |
| `.env` | **All API keys/secrets** — the one blessed spot, loaded on every entry point. `chmod 600`. See [`docs/wiki/security/secrets.md`](docs/wiki/security/secrets.md) |
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
- **Unattended guardrail** — scheduler dispatches are marked `AGENTWIRE_UNATTENDED=1`; the hook resolves `ask`-tier commands by failing closed (block + email owner) unless on the `unattended_allow` list. See [`docs/wiki/internals/damage-control.md`](docs/wiki/internals/damage-control.md).
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

LLM-maintained knowledge base at `~/.agentwire/wiki/` using the Karpathy LLM Wiki pattern. Research and debugging knowledge compounds across sessions. **Authoring is in-context** — the session that learns something writes the page itself (there's no batch ingester); the *mechanical* ops are deterministic, via the `agentwire wiki` CLI and the `wiki_query` / `wiki_lint` / `wiki_status` MCP tools (`status` / `query` / `lint` / `new` / `done` — stdlib-only, no rebuild needed; see the `wiki` skill). **Before researching**: agents call `wiki_query` first. After discovering: agents write/update a page (`agentwire wiki new` scaffolds it). `raw/` is an optional verbatim-source inbox; archive a consumed source with `agentwire wiki done`.

## Handoffs

`/handoff` distills the current conversation into a shareable bundle: `ai-handoff.md` for another LLM and `show-the-story.html` for humans. The agent does the distillation in-context (free); the CLI/MCP renders deterministically. Outputs in `~/.agentwire/artifacts/handoff-<slug>/`. Full reference: [`docs/wiki/communication/handoff.md`](docs/wiki/communication/handoff.md).

## Usage-Limit Recovery

Deterministic (zero-LLM) recovery from the Claude Code usage-limit dialog: a launchd watchdog (`agentwire limits tick`, 60s) plus ensure's completion poll detect the dialog, park the session (option 1: stop and wait), parse the reset time, email the owner via Resend, and nudge the session to continue after reset. Park state under `~/.agentwire/usage-limit/` guards every surface — ensure exits 7 (`usage_limit`), the scheduler skips dispatch, the idle hook never reaps a parked session. CLI under `agentwire limits ...`. Full reference: [`docs/wiki/usage-limit-recovery.md`](docs/wiki/usage-limit-recovery.md).

## Prompt Routing

Interactive prompts (permission, plan-approval, AskUserQuestion) hitting a child session are routed as text to its parent/orchestrator — hook path for permissions (seconds), pane-sweep riding the limits watchdog for the rest (≤60s). Parent resolution: worker pane → pane 0; else creator recorded at `agentwire new`; else `.agentwire.yml` `parent:`. Answer ONLY with `agentwire prompts answer -s <session> --expect <hash> <key>` (compare-and-send; raw send-keys races the portal). Deliveries are safety-gated (never paste into a live menu, a bare shell, or a parked session). CLI under `agentwire prompts ...`. Full reference: [`docs/wiki/sessions/prompt-routing.md`](docs/wiki/sessions/prompt-routing.md).

## Polite Messaging

`agentwire msg` is the non-interrupting sibling of `agentwire send`. `send`/`session_send` paste into the prompt and press Enter immediately — and **clobber a human's half-typed draft** if the box is occupied. `msg` drops a typed JSON message into a per-recipient file inbox (`~/.agentwire/inbox/<session>/`) and the watchdog injects it **only when the input box is empty** (`prompt_router.prompt_is_empty`) and `safe_deliver` guards pass (not parked, agent pane, no live dialog). Drain rides the limits watchdog (≤60s), coalescing queued messages into one paste; deferred messages bump `attempts` and dead-letter after 40. Typed kinds (`note`/`done`/`request`/`escalation`), `@all` broadcast (live agent sessions minus sender). Dead-lettered messages carry their drop reason + timestamp; `agentwire msg dead [-s <session>]` (MCP `msg_dead`) lists them so the drop is never silent. CLI `agentwire msg send|inbox|dead|flush`, MCP `msg_send`/`msg_inbox`/`msg_dead`. **Use `msg` for routine peer updates; reserve `send`/`session_send` for forcibly driving a session now.** Full reference: [`docs/wiki/sessions/messaging.md`](docs/wiki/sessions/messaging.md).

## Council

Multi-soul orchestrator sitting, **namespaced by `<name>`** so independent councils run concurrently: `agentwire-council-<name>` fans prompts out to lens sessions (`council-<name>-brain`, `council-<name>-conscience`, …), each replying take/ack/pass through a file inbox under `~/.agentwire/council/<name>/prompts/`; the orchestrator collects and synthesizes with attribution. Targeting: `--name` → cwd-repo-slug if it matches a live sitting → sole live sitting → else error+list; every command echoes which sitting it hit. The standard `soul` role self-excludes from any `council-*` session. CLI under `agentwire council ...` (incl. `council list`), 6 MCP `council_*` tools. Full reference: [`docs/wiki/council.md`](docs/wiki/council.md).

## Briefing Mode

Asymmetric-verbosity orchestration: a terse, human-facing **`anchor`** (persona role; replaces orchestrator) fans out exhaustively-verbose **`correspondent`** worktrees (`worktree_create(roles="correspondent", prompt=…)`; stacks on the worktree-session rail). Correspondents file deep reports into the dropbox (`agentwire research ensure` / `research_dir()` → `~/.agentwire/research/<anchor>/`) and signal **passively** — `msg send --kind ingest --ref <path>` is never auto-delivered, so it never drives the anchor. The anchor stays quiet until the human cues it, then `msg pull`s the pointers, reads the files, and **briefs across two channels in one call**: `say(text="<spoken headline>", display="<richer toast card>")` — different content per channel; the toast (`notify_user`) renders a safe markdown subset. Teardown via `worktree_remove` / `worktree_prune`. The notify-* family is split by target: `notify_user` (human toast), `notify_parent` (your orchestrator), `notify_event` (portal lifecycle). Full reference: [`docs/wiki/briefing-mode.md`](docs/wiki/briefing-mode.md).

## Reference Skills

Reference detail lives in skills under `.claude/skills/` — invoke as needed:

| Skill | When to use |
|-------|-------------|
| `agentwire-cli` | Running or composing any `agentwire ...` shell command |
| `agentwire-mcp-tools` | Picking the right MCP tool from inside an agent session |
| `agentwire-config` | Editing `~/.agentwire/config.yaml` (TTS, channels, services, etc.) |
| `agentwire-project-config` | Editing `.agentwire.yml`, defining tasks, roles, idle notifications |
| `agentwire-scheduler` | Scheduled task gates/schedule/priority |
| `agentwire-desktop-ui` | Editing portal static files (sidebar, windows, artifacts) |
| `agentwire-pi` | Setting up pi sessions (zai, deepseek, openai, etc.) via pi coding agent |

## Docs

- CLI: `agentwire --help` or `agentwire <cmd> --help`
- **[`docs/wiki/INDEX.md`](docs/wiki/INDEX.md)** — feature reference manual (sessions, communication, scheduling, integrations, deployment, TTS, internals)

## Issue tracking

How work is tracked is a contributor preference, not something agentwire ships — the product imposes no PM mandate. For *this* repo, the convention is GitHub issues as the source of truth, with `Closes #N` in the PR body to link and auto-close. If you keep cross-repo tracking preferences in `~/.claude/rules/project-tracking.md`, those govern your own workflow and override this. Post-ship reference content (concepts, architecture, troubleshooting) belongs in `docs/wiki/`.
