# Architecture

> Living document. Update this, don't create new versions.

A single-page reference for how AgentWire's pieces fit together. For deep dives on any one piece, follow the links into the rest of the wiki.

---

## Process Model

tmux is the substrate. One agentwire session = one tmux session. Inside that session, panes are organized as orchestrator + workers:

```
tmux session "myproject"
├── pane 0  → orchestrator   (Claude Code, claudeglm, pi, sdk-*)
├── pane 1  → worker          (spawned via pane_spawn, auto-kills on idle)
├── pane 2  → worker
└── ...
```

The orchestrator coordinates work and dispatches workers via the MCP `pane_spawn` tool. Workers fire an *idle notification* on completion (via `~/.claude/hooks/idle-handler.sh`); the hook routes the alert to pane 0 and kills the worker. Pane 0's own idle notifications route to whatever session is named in `parent:` (typically the human-facing session).

For session types — claude-bypass, claude-auto, claudeglm-*, pi-*, sdk-*, bare — see [Sessions index](INDEX.md#sessions). For the worker-pane lifecycle in detail, see [CLAUDE.md](../../CLAUDE.md#worker-pane-lifecycle).

---

## CLI / Portal / MCP

Three surfaces, one source of truth.

```
┌──────────────────────────────┐  ┌──────────────────────────────┐
│  Humans / scripts            │  │  Agents inside sessions      │
│    agentwire <cmd>           │  │    MCP tools (87 of them)    │
└─────────────┬────────────────┘  └─────────────┬────────────────┘
              │                                 │
              ▼                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  agentwire CLI  (agentwire/__main__.py)                         │
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
├── config.yaml              # main config (TTS, channels, services, pi providers, …)
├── machines.json            # remote machines registry
├── scheduler.yaml           # scheduled tasks
├── scheduler-events.jsonl   # scheduler audit log
├── overnight-events.jsonl   # overnight queue audit log
├── roles/                   # role files (system-prompt personas)
├── voices/                  # TTS reference WAVs
├── damage-control/          # OPTIONAL user override for security rules
├── apps/, artifacts/        # agent-generated UIs and HTML artifacts
├── locks/                   # session mutexes (acquired by `agentwire ensure`)
├── queues/                  # overnight queue items
├── sessions/                # SDK session transcripts (REPL persistence)
├── sdk-sessions/            # SDK conversation forks
├── tasks/                   # ensure-task summary files
├── tooldefs/                # tool definitions for damage-control ask-patterns
├── tunnels/                 # SSH tunnel state
├── logs/                    # damage-control audit logs (per-day JSONL)
├── docs/, scripts/          # wiki + machine-specific helpers (local, not synced)
└── cert.pem, key.pem        # self-signed TLS for the portal
```

### Per-project — `.agentwire.yml`

Lives at the project root. Defines the session type, roles, voice, parent (for cross-session notifications), and named tasks. See `agentwire-project-config` skill for the full schema.

```yaml
type: claude-auto
roles: [task-runner]
voice: may
parent: main

tasks:
  nightly-tests:
    starting_ref: main
    prompt: "Run tests, fix failures, open a draft PR."
```

---

## Communication Graph

```
       External platforms (channels)             Voice / audio (primitives)
       ───────────────────────────────           ──────────────────────────
       Discord, Slack, Telegram (bridges)        TTS server (port 8100)
       Email, SMS, Webhook, Quo (send-only)      STT server (whisperkit / faster-whisper)
              │              ▲                          │            ▲
              ▼              │                          ▼            │
       ┌─────────────────────────────────────────────────────────────────┐
       │                      AgentWire sessions                          │
       │                                                                  │
       │   parent: main ◄───── orchestrator ──── pane_spawn ──► worker   │
       │                            │                              │     │
       │                            └───── idle notifications ◄────┘     │
       └──────────────────────────────────────────────────────────────────┘
                                    ▲ ▼
                          smart audio routing
                          (browser if connected, else local speakers)
```

- **Channels** route inbound messages from external platforms into specific sessions, and forward outbound events (alerts, AskUserQuestion, voice) back out. → [Channels](communication/channels.md).
- **Voice and STT** are *primitives*, not channels: any channel can call `self.tts(...)` and `self.stt(...)` from the base class.
- **Idle notifications** form a tree: workers → pane 0 of the same session → the `parent:` session (typically human-facing). This is what makes hierarchical multi-session orchestration tractable.

---

## Scheduling / Workflows / Overnight

Three execution paths for non-interactive work, each picked per use case:

| Path | Field | Dispatch | Best for |
|---|---|---|---|
| **Ensure task** | `task: <name>` in scheduler.yaml + `tasks: <name>:` in .agentwire.yml | `agentwire ensure` → tmux session → Claude Code | Multi-step agent work needing branch/PR/MCP tools |
| **Workflow task** | `workflow: <name>` in scheduler.yaml | `run_workflow()` in-process → pi or anthropic-runner subprocesses per node | Deterministic DAGs of small reliable nodes |
| **Overnight queue** | `agentwire overnight prepare …` | Orchestrator dispatches forked Claude sessions during a configured window | Human-prepared one-shot work that can't be expressed as recurring YAML |

The scheduler handles ensure + workflow tasks. The overnight queue is **separate**: it dispatches sessions outside the scheduler's task model, with full forked Claude conversation context as the entry point.

```
                ┌──────────── ~/.agentwire/scheduler.yaml ────────────┐
                │   tasks:                                            │
                │     nightly-tests:    task: write-tests             │
                │     nightly-doc-drift: workflow: doc-drift-check    │
                └──────────────┬─────────────────┬────────────────────┘
                               ▼                 ▼
                  agentwire ensure       run_workflow() in-process
                  (tmux + Claude)        (pi or anthropic-runner per node)

                ┌── ~/.agentwire/queues/  (overnight prepare queue) ──┐
                │  human prepares interactively → captured sessionId  │
                └──────────────┬──────────────────────────────────────┘
                               ▼
                    overnight orchestrator dispatches inside window
                    (forks Claude context, runs to completion, opens PR)
```

Decision shortcut:
- Recurring + autonomous → scheduler with `task:` or `workflow:`.
- One-shot, judgment-heavy → overnight queue.
- Ad-hoc → `agentwire ensure` or just open a session.

→ [Scheduled workloads](scheduling/scheduled-workloads.md), [Pi workflows](scheduling/workflows.md).

---

## Safety

Defense in depth, three layers:

1. **Damage control hooks** (always on if `agentwire hooks install` was run): PreToolUse hooks on Bash/Edit/Write match commands and paths against `agentwire/hooks/damage-control/rules/*.yaml`. Block hard-blocked patterns, prompt for ask-patterns, run bypassable patterns through allowlist checks.
2. **Per-project allowlists** (`safety.allowed_paths` in `.agentwire.yml`): override the global rules for paths inside this project (e.g., `dist/*` allow-all, `.env.development` allow read/write/edit).
3. **Classifier-mode auto sessions** (`type: claude-auto`): a Sonnet 4.6 classifier reviews each tool call before execution. Safe ops auto-approve at zero cost; dangerous ops are blocked. Layered on top of the hook-level checks.

→ [Damage control](internals/damage-control.md), [claude-auto](sessions/claude-code-auto-mode.md).

---

## Where to Go Next

- New term unfamiliar? → [Glossary](glossary.md).
- Building a session? → [Sessions index](INDEX.md#sessions).
- Defining a recurring task? → [Scheduled workloads](scheduling/scheduled-workloads.md).
- Wiring a channel? → [Channels](communication/channels.md).
- Debugging? → [Troubleshooting](internals/troubleshooting.md).
