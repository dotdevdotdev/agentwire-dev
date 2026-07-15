# Architecture

> Living document. Update this, don't create new versions.

A single-page reference for how AgentWire's pieces fit together. For deep dives on any one piece, follow the links into the rest of the wiki.

---

## Process Model

tmux is the substrate. One agentwire session = one tmux session. Inside that session, panes are organized as orchestrator + workers:

```
tmux session "myproject"
├── pane 0  → orchestrator   (Claude Code)
├── pane 1  → worker          (spawned via pane_spawn, auto-kills on idle)
├── pane 2  → worker
└── ...
```

The orchestrator coordinates work and dispatches workers via the MCP `pane_spawn` tool. Workers fire an *idle notification* on completion (via `~/.claude/hooks/idle-handler.sh`); the hook routes the alert to pane 0 and kills the worker. Pane 0's own idle notifications route to whatever session is named in `parent:` (typically the human-facing session).

Role (orchestrator/worker) and topology (main/worktree/pane) are independent axes (#716): a worker doesn't have to live in a pane — `agentwire worktree <name>` spawns it as a standalone tmux session instead, pushing a branch and opening a draft PR on completion rather than firing a pane-idle notification. The pane diagram above is the pane-topology case; see [worktree sessions](sessions/worktree-sessions.md) for the other.

For postures — bypass, prompted, auto (or bare) — see [Sessions index](INDEX.md#sessions). For the worker-pane lifecycle in detail, see [CLAUDE.md](../../CLAUDE.md#worker-pane-lifecycle).

---

## CLI / Portal / MCP

Three surfaces, one source of truth.

```
┌──────────────────────────────┐  ┌──────────────────────────────┐
│  Humans / scripts            │  │  Agents inside sessions      │
│    agentwire <cmd>           │  │    MCP tools (107 of them)   │
└─────────────┬────────────────┘  └─────────────┬────────────────┘
              │                                 │
              ▼                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  agentwire CLI  (agentwire/core.py + agentwire/<domain>_cli.py) │
│  • single source of truth for all session/machine/task logic    │
│  • every command supports --json for machine-readable output    │
└────────────────┬────────────────────────────────────────────────┘
                 │
   ┌─────────────┴────────────┐
   ▼                          ▼
Portal (server.py)       Direct subprocess
• REST + WebSocket        (humans, hooks,
• calls run_agentwire_cmd  scheduler, MCP)
• never reimplements
  business logic
```

**Rules:**
1. New behavior implements first in the CLI with `--json` output.
2. The portal calls the CLI via `run_agentwire_cmd(["cmd", "args"])` and parses the JSON result. It adds WebSocket / real-time / browser layers on top, never reimplements logic.
3. Agents inside sessions reach for MCP tools, not the CLI. Same logic underneath; nicer ergonomics for agents. `agentwire-mcp-tools` skill has the full surface.

This is why bug fixes land in one place: change the CLI, the portal and MCP tools both pick it up after `agentwire rebuild && agentwire portal restart --dev`.

---

## Storage Layout

### Global — `~/.agentwire/`

```
~/.agentwire/
├── config.yaml              # main config (TTS, STT, channels, services, session/posture defaults, …)
├── .env                     # all API keys/secrets, chmod 600 (see security/secrets.md)
├── machines.json            # remote machines registry
├── scheduler.yaml           # scheduled tasks
├── scheduler-events.jsonl   # scheduler audit log
├── roles/                   # role files (system-prompt personas)
├── voices/                  # TTS reference WAVs
├── damage-control/          # OPTIONAL user override for security rules
├── apps/, artifacts/        # agent-generated UIs and HTML artifacts
├── locks/                   # session mutexes (acquired by `agentwire ensure`)
├── sessions/                # per-session metadata.json
├── usage-limit/             # usage-limit recovery park state
├── tasks/                   # ensure-task summary files
├── tooldefs/                # tool definitions for damage-control ask-patterns
├── tunnels/                 # SSH tunnel state
├── logs/                    # damage-control audit logs (per-day JSONL)
├── wiki/, scripts/          # wiki knowledge base + machine-specific helpers (local, not synced)
└── cert.pem, key.pem        # self-signed TLS for the portal
```

### Per-project — `.agentwire.yml` + `.agentwire.tasks.yml`

`.agentwire.yml` lives at the project root and is purely declarative: posture, roles, voice, parent (for cross-session notifications), worktree overrides — no execution vector, so it's agent-writable. Named tasks (`pre`/`post`/`on_task_end`/`shell` — code the scheduler runs via `shell=True`) live in a separate, protected sibling file, `.agentwire.tasks.yml` (#720), authored via propose-and-promote (`agentwire tasks review` / `agentwire tasks promote`) since a policed agent can't write it directly. See `agentwire-project-config` skill for the full schema and [Damage control](internals/damage-control.md#task-execution-config-split-agentwiretasksyml-720) for the protection model.

**Gitignore both.** They're personal/live config (voices, schedules, notification addresses, task shell commands) — not project code. Tracking either also breaks worktree dispatch subtly: worktree runs check out HEAD, so uncommitted live edits to a tracked file are silently ignored. Gitignored, they're seeded into worktrees via `projects.worktrees.copy_files` (default includes both), so the live file always wins. AgentWire adds `.agentwire.yml` to `.gitignore` automatically whenever it writes the file into a git repo; `agentwire tasks promote` does the same for `.agentwire.tasks.yml` on first promote.

```yaml
# .agentwire.yml
posture: auto
roles: [task-runner]
voice: may
parent: main
```

```yaml
# .agentwire.tasks.yml
tasks:
  nightly-tests:
    starting_ref: main
    prompt: "Run tests, fix failures, open a draft PR."
```

---

## Communication Graph

```
       Outbound channels (send-only)            Voice / audio (primitives)
       ───────────────────────────────          ──────────────────────────
       Email (Resend), Quo / OpenPhone SMS      TTS server (port 8100)
                  ▲                              STT shim (moonshine / faster-whisper)
                  │                                       │            ▲
                  │ outbound notifications                ▼            │
       ┌─────────────────────────────────────────────────────────────────┐
       │                      AgentWire sessions                          │
       │                                                                  │
       │   parent: main ◄───── orchestrator ──── pane_spawn ──► worker   │
       │                            │                              │     │
       │                            └───── idle notifications ◄────┘     │
       └─────────────────────────────────────────────────────────────────┘
                                    ▲ ▼
                          smart audio routing                inbound input
                  (browser if connected, else local)     ←── via portal WS
```

- **Channels** are outbound-only notification integrations — a session calls `agentwire email` or `agentwire quo` to push a notification out; a third channel, `push` (Web Push/VAPID), auto-mirrors portal toasts to subscribed devices rather than being invoked directly. Inbound user input flows through the portal (web + tunnel), not channels. Inbound chat-platform bridges (Telegram, Discord, Slack) were removed; the portal is the single inbound surface. → [Channels](communication/channels.md).
- **Voice and STT** live on the portal side as `say()` / `listen()` agent tools.
- **Idle notifications** form a tree: workers → pane 0 of the same session → the `parent:` session (typically human-facing). This is what makes hierarchical multi-session orchestration tractable.

---

## Scheduling

Non-interactive work runs through the scheduler:

| Path | Field | Dispatch | Best for |
|---|---|---|---|
| **Ensure task** | `task: <name>` in scheduler.yaml + `tasks: <name>:` in .agentwire.tasks.yml | `agentwire ensure` → tmux session → Claude Code | Recurring agent work |

```
                ┌──────── ~/.agentwire/scheduler.yaml ─────────┐
                │   tasks:                                     │
                │     nightly-tests:    task: write-tests      │
                └──────────────────┬───────────────────────────┘
                                   ▼
                          agentwire ensure
                          (tmux + Claude)
```

Decision shortcut:
- Recurring + autonomous → scheduler with `task:`.
- Ad-hoc → `agentwire ensure` or just open a session.

→ [Scheduled workloads](scheduling/scheduled-workloads.md).

---

## Safety

Defense in depth, three layers:

1. **Damage control hooks** (always on if `agentwire hooks install` was run): PreToolUse hooks on Bash/Edit/Write/Read/Grep/Glob match commands and paths against `agentwire/hooks/damage-control/rules/*.yaml`. Block hard-blocked patterns, prompt for ask-patterns, run bypassable patterns through allowlist checks; the read-side hook additionally enforces `zeroAccessPaths` on content reads.
2. **Per-project allowlists** (`allowed_paths` in the protected `.damagecontrol.yml` at the repo root): override the global rules for paths inside this project (e.g., `dist/*` allow-all, `.env.development` allow read/write/edit). The allowlist is host-owned — an agent can't edit `.damagecontrol.yml` to widen its own freedom (#466/#467).
3. **Classifier-mode auto sessions** (`posture: auto`): a Sonnet 4.6 classifier reviews each tool call before execution. Safe ops auto-approve at zero cost; dangerous ops are blocked. Layered on top of the hook-level checks.

→ [Damage control](internals/damage-control.md), [auto](sessions/claude-code-auto-mode.md).

---

## Where to Go Next

- New term unfamiliar? → [Glossary](glossary.md).
- Building a session? → [Sessions index](INDEX.md#sessions).
- Defining a recurring task? → [Scheduled workloads](scheduling/scheduled-workloads.md).
- Wiring a channel? → [Channels](communication/channels.md).
- Debugging? → [Troubleshooting](internals/troubleshooting.md).
