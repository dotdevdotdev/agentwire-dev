---
name: agentwire-project-config
description: Reference for per-project `.agentwire.yml` — session type, roles, voice, parent, shell, safety allowlist; task schema with pre/prompt/post/output/branch-management fields and built-in variables; hierarchical idle notifications with worker summary files and queue processor; role system. Use when configuring a project for agentwire, wiring up scheduled tasks, defining worker/orchestrator relationships, or debugging task execution.
---

# `.agentwire.yml` Project Config

Each project can have a `.agentwire.yml` in its root directory. This configures session type, roles, voice, and parent for that project.

**Format is FLAT (no nesting):**

```yaml
# Session with voice and agentwire awareness
type: claude-bypass
roles:
  - agentwire
  - voice
voice: may
parent: main  # Notify parent session when idle (optional)
```

```yaml
# WRONG - don't nest under "session:"
session:
  type: claude
  roles: [...]  # This won't be loaded!
```

| Field | Values | Description |
|-------|--------|-------------|
| `type` | `claude-bypass`, `claude-auto`, `claude-prompted`, `claude-restricted`, `pi-zai`, `pi-zai-restricted`, `pi-zai-readonly`, `sdk-bypass`, `sdk-prompted`, `sdk-restricted` | Session permission level. **Use `claude-auto` for overnight/unattended work** — same capability as `claude-bypass` but with AI classifier blocking dangerous actions. Requires Team/Enterprise plan. **`sdk-*` types** spawn the Textual `agentwire repl` (claude-agent-sdk via Anthropic subscription) — see `docs/repl-tui.md`. |
| `roles` | List of role names | Roles to load (from bundled or `~/.agentwire/roles/`) |
| `voice` | Voice name | TTS voice for this project |
| `parent` | Session name | Parent session for hierarchical notifications |
| `shell` | `/bin/sh`, `/bin/bash`, etc. | Default shell for task commands |
| `tasks` | Task definitions | Scheduled workload configurations |
| `safety` | `{allowed_paths: [...]}` | Per-project damage control allowlist |

For Z.AI sessions (`pi-zai*`), see the `agentwire-pi-zai` skill. For
the Textual REPL surface (`sdk-*` types or `agentwire repl` directly),
see `docs/repl-tui.md` — it covers slash commands, the `--view fanout`
multi-generation A/B view, theming, and the portal watch-mode.

## Task Schema

Tasks are defined in `.agentwire.yml` for use with `agentwire ensure`:

```yaml
shell: /bin/sh  # Project-level default shell

tasks:
  morning-briefing:
    shell: /bin/bash           # Task-level override
    priority: 10               # Pipeline ordering (lower = higher priority, default: 99)
    retries: 2                 # Retry on failure (default: 0)
    retry_delay: 30            # Seconds between retries (default: 30)
    idle_timeout: 30           # Seconds of idle before completion (default: 30)
    exit_on_complete: true     # Exit session after completion (default: true)
    role: task-runner          # Role override for this task (optional)
    pre:                       # Data gathering (NO {{ }} - these PRODUCE variables)
      weather: "curl -s wttr.in/?format=3"
      calendar:
        cmd: "gcal-cli today --json"
        required: true         # Fail if empty (default: false)
        validate: "jq . > /dev/null"  # Validation command
        timeout: 30            # Command timeout
    prompt: |                  # Main prompt (supports {{ variables }})
      Weather: {{ weather }}
      Calendar: {{ calendar }}
      Summarize my day.
    on_task_end: |             # Optional: after system summary
      Read {{ summary_file }}.
      If complete, save to ~/briefings/{{ date }}.md
    post:                      # Commands after completion
      - "echo 'Status: {{ status }}'"
    output:
      capture: 50              # Lines to capture
      save: ~/logs/{{ task }}.log
      notify: voice            # voice, alert, webhook ${URL}, command "..."

    # Branch management (for overnight/async agent workflows)
    starting_ref: main         # Git ref to checkout before task runs
    work_branch: agent/task    # Branch for agent's work (default: agent/<task>-<date>)
    pr_target: main            # PR target branch (default: starting_ref)
    pr_draft: true             # Create as draft PR (default: true)

    # Context inheritance
    starting_session: ctx-loaded  # Fork Claude context from this session before running
```

**Built-in variables:**
- `{{ date }}`, `{{ time }}`, `{{ datetime }}` - Current date/time
- `{{ session }}`, `{{ task }}`, `{{ project_root }}` - Task identity
- `{{ attempt }}` - Current attempt number (1-based)
- `{{ status }}`, `{{ summary }}`, `{{ summary_file }}` - After completion
- `{{ output }}` - Captured session output (in post phase)
- `{{ work_branch }}`, `{{ pr_url }}` - Branch/PR after branch management (in post phase)
- `{{ var_name }}` - Pre-command outputs

**Exit codes:** 0=complete, 1=failed, 2=incomplete, 3=lock conflict, 4=pre failure, 5=timeout, 6=session error

## Hierarchical Idle Notifications

When a session goes idle, it notifies up the hierarchy via `agentwire notify-parent` (text-only, no audio):

```
parent session ← receives "[ALERT from child] ..."
    ↑ notify-parent --to parent
child session   ← receives "[ALERT from pane N] ..."
    ↑ auto-notify pane 0
worker panes
```

**Worker summary files:**
- Workers write summaries to `.agentwire/worker-{pane}.md` before going idle
- Summaries include: task, status, what worked, what didn't, notes for orchestrator
- Orchestrators read these files to understand worker results

**Auto-exit (workers auto-kill on idle):**
- Worker panes (index > 0) automatically exit when idle
- Use `parent: <session-name>` in `.agentwire.yml` for child → parent notifications

**Queue system files:**
- `~/.agentwire/queue-processor.sh` - Processes queue with 15s delays between alerts
- `~/.agentwire/queues/{session}.jsonl` - Per-session notification queues

**Worker idle sequence:**
1. `session.idle` fires → wait 2s (let agent settle)
2. Worker writes summary to `.agentwire/worker-{pane}.md`
3. Queue notification to `{session}.jsonl`
4. Start queue processor if not running
5. Worker auto-exits

**Idle notifications** are handled via `~/.claude/hooks/idle-handler.sh`.

**Creating a project with roles:**

```bash
# Option 1: Create .agentwire.yml first, then create session
echo "type: claude-bypass
roles:
  - agentwire
  - voice" > ~/projects/myproject/.agentwire.yml

agentwire new -s myproject -p ~/projects/myproject

# Option 2: Specify roles on command line (saves to .agentwire.yml)
agentwire new -s myproject -p ~/projects/myproject --roles agentwire,voice
```

By default, `agentwire new --type X` is a session-level override only and never saves to `.agentwire.yml`. Use `--persist` to opt in to saving.

## Role System

Roles define agent behavior and are composable. Mix and match roles in `.agentwire.yml` to configure orchestrators, workers, or specialized agents.

**Available roles:**

| Role | Purpose |
|------|---------|
| `agentwire` | Core session/pane/MCP tools awareness |
| `orchestrator` | Long-lived project orchestrator — plans, delegates, manages overnight queue |
| `voice` | Voice communication (speak/listen) |
| `worker` | Receive tasks, execute autonomously, report back |
| `task-runner` | Scheduled task execution |
| `chatbot` | Conversational personality |
| `init` | Setup wizard behavior |
| `slack-dm` | Slack bot — reply()-based conversation with Slack users |
| `discord-dm` | Discord bot — reply()-based conversation with Discord users |
| `channel-admin` | Self-configure channel setup via chat (edit config.yaml, restart bridges) |

Use `agentwire roles list` to see available roles. Roles are bundled in `agentwire/roles/` and can be composed freely in `.agentwire.yml`.
