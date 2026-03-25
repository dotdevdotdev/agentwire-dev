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
```

## CLI is the Single Source of Truth

**Always use `agentwire` CLI for session management.** The CLI is the authoritative interface - the web portal wraps CLI commands via `run_agentwire_cmd()`.

### Architecture Principle

All session/machine logic lives in CLI commands (`__main__.py`). The portal (`server.py`) is a thin wrapper that:
1. Calls CLI via `run_agentwire_cmd(["command", "args"])`
2. Parses JSON output (`--json` flag)
3. Adds WebSocket/real-time features

**When adding new functionality:**
1. Implement in CLI first with `--json` output
2. Portal calls CLI, doesn't duplicate logic
3. Never bypass CLI with direct tmux/subprocess calls

### CLI Commands

```bash
# Session management
agentwire new -s name           # not: tmux new-session
agentwire send -s name "prompt" # not: tmux send-keys
agentwire send-keys -s name key1 key2  # raw keys with pauses
agentwire output -s name        # not: tmux capture-pane
agentwire info -s name          # session metadata (cwd, panes) as JSON
agentwire kill -s name          # not: tmux kill-session
agentwire list                  # not: tmux list-sessions
agentwire recreate -s name      # destroy and recreate with fresh worktree
agentwire fork -s name          # fork session into new worktree
agentwire fork -s name -t project/branch --commit abc123  # fork from specific commit

# Pane commands (for workers within same session)
agentwire spawn --roles worker  # spawn worker pane
agentwire send --pane 1 "task"  # send to pane
agentwire output --pane 1       # read pane output
agentwire kill --pane 1         # kill pane
agentwire jump --pane 1         # focus pane
agentwire split -s name         # add terminal pane(s)
agentwire detach -s name        # move pane to its own session
agentwire resize -s name        # resize window to fit largest client

# Portal management
agentwire portal start          # start in tmux
agentwire portal stop           # stop portal
agentwire portal restart        # stop + start
agentwire portal status         # check health

# TTS/STT servers
agentwire tts start|stop|status # TTS server management
agentwire stt start|stop|status # STT server management

# Voice
agentwire say "text"            # speak (auto-routes to browser or local)
agentwire say -s name "text"    # speak to specific session
agentwire alert "text"          # text notification to parent (no audio)
agentwire alert --to name "text" # text notification to specific session
agentwire listen start|stop|cancel  # voice recording

# Voice cloning
agentwire voiceclone start      # start recording voice sample
agentwire voiceclone stop name  # stop and save as voice clone
agentwire voiceclone list       # list available voices
agentwire voiceclone delete name # delete a voice clone

# Artifact windows (agent visual canvas)
agentwire open <url> --title "T"  # open URL or local file as artifact window
agentwire open dashboard.html     # open from ~/.agentwire/artifacts/

# Email notifications
agentwire email --to addr --subject "Subject" --body "Body"
agentwire email --body "msg" # uses default_to from config
agentwire email --attach file.pdf --body "See attached"

# Telegram bridge
agentwire telegram start       # start bot in tmux
agentwire telegram stop        # stop bot
agentwire telegram serve       # run bot in foreground
agentwire telegram status      # check bot status

# Machine management
agentwire machine list
agentwire machine add <id> --host <host> --user <user>
agentwire machine remove <id>

# SSH tunnels (for remote services)
agentwire tunnels up            # create all required tunnels
agentwire tunnels down          # tear down all tunnels
agentwire tunnels status        # show tunnel health
agentwire tunnels check         # verify tunnels are working

# Lock management (for scheduled tasks)
agentwire lock list             # list all locks
agentwire lock clean            # remove stale locks
agentwire lock remove <session> # force-remove a specific lock

# Project discovery
agentwire projects list         # discover projects from projects_dir
agentwire projects list --json  # JSON output for scripting

# Session history
agentwire history list          # list conversation history
agentwire history show <id>     # show session details
agentwire history resume <id>   # resume session (always forks)

# Roles management
agentwire roles list            # list available roles
agentwire roles show <name>     # show role details

# Scheduled workloads
agentwire ensure -s name --task task  # run named task reliably
agentwire task list [session]         # list tasks for session/project
agentwire task show session/task      # show task definition
agentwire task validate session/task  # validate task syntax

# Safety & diagnostics
agentwire safety check "cmd"    # test if command would be blocked
agentwire safety status         # show pattern counts and recent blocks
agentwire safety logs           # query audit logs
agentwire safety install        # install damage control hooks
agentwire hooks install         # install permission hook (Claude Code only)
agentwire hooks uninstall       # remove permission hook (Claude Code only)
agentwire hooks status          # check hook installation status
agentwire network status        # complete network health check
agentwire doctor                # auto-diagnose and fix issues

# Notifications
agentwire notify event          # notify portal of state changes (session/pane events)

# MCP Server
agentwire mcp                   # expose agentwire as MCP server

# Scheduler
agentwire scheduler start|serve|stop|status # manage scheduler daemon
agentwire scheduler board                   # show task board with overdue scores
agentwire scheduler live                    # show live scheduler state
agentwire scheduler events                  # show recent scheduler events
agentwire scheduler history                 # show recent run history
agentwire scheduler run task                # force-run a task now
agentwire scheduler enable|disable task     # enable/disable a task
agentwire scheduler report [--since 8h] [--artifact]  # generate morning report HTML
agentwire scheduler dashboard               # open scheduler dashboard

# Setup & Development
agentwire init                  # interactive setup wizard
agentwire generate-certs        # generate SSL certificates
agentwire dev                   # start/attach to dev session
agentwire rebuild               # clear uv cache and reinstall
agentwire uninstall             # uninstall the tool
```

Session formats: `name`, `project/branch` (worktree), `name@machine` (remote)
Pane targeting: `--pane N` auto-detects session from `$TMUX_PANE`

For CLI details: `agentwire --help` or `agentwire <cmd> --help`

## MCP Server (For Agents)

**Agents running in agentwire sessions should use MCP tools instead of CLI commands.**

The agentwire MCP server provides tools that wrap CLI functionality. Use these instead of `Bash: agentwire <cmd>`:

### Session Management (9 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire list` | `sessions_list()` |
| `agentwire new -s name` | `session_create(name="...")` |
| `agentwire send -s name "msg"` | `session_send(session="...", message="...")` |
| `agentwire output -s name` | `session_output(session="...")` |
| `agentwire info -s name` | `session_info(session="...")` |
| `agentwire kill -s name` | `session_kill(session="...")` |
| `agentwire send-keys -s name key1 key2` | `session_send_keys(session="...", keys=["..."])` |
| `agentwire recreate -s name` | `session_recreate(session="...")` |
| `agentwire fork -s name -t project/branch` | `session_fork(session="...", target="...")` |
| `agentwire fork -s name -t project/branch --commit abc` | `session_fork(session="...", target="...", commit="abc")` |

### Pane Management (9 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire spawn --roles worker` | `pane_spawn(roles="worker")` |
| `agentwire send --pane 1 "msg"` | `pane_send(pane=1, message="...")` |
| `agentwire output --pane 1` | `pane_output(pane=1)` |
| `agentwire kill --pane 1` | `pane_kill(pane=1)` |
| `agentwire list` (in tmux) | `panes_list()` |
| `agentwire split -n 2` | `pane_split(count=2)` |
| `agentwire detach --pane 1 -s target` | `pane_detach(session="src", pane=1, target="target")` |
| `agentwire jump --pane 1` | `pane_jump(pane=1)` |
| `agentwire resize` | `pane_resize()` |

### Voice & TTS (12 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire say "text"` | `say(text="...")` |
| `agentwire alert "text"` | `alert(text="...")` |
| `agentwire listen start` | `listen_start()` |
| `agentwire listen stop` | `listen_stop()` |
| `agentwire listen cancel` | `listen_cancel()` |
| `agentwire voiceclone start` | `voiceclone_start()` |
| `agentwire voiceclone stop name` | `voiceclone_stop(name="...")` |
| `agentwire voiceclone cancel` | `voiceclone_cancel()` |
| `agentwire voiceclone list` | `voiceclone_list()` |
| `agentwire voiceclone delete name` | `voiceclone_delete(name="...")` |
| (portal API) | `transcribe(audio_base64="...", format="webm")` |
| `agentwire voiceclone list` | `voices_list()` |

### Tasks & Locks (7 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire ensure -s x --task y` | `task_run(session="x", task="y")` |
| `agentwire task list x` | `task_list(session="x")` |
| `agentwire task show x/y` | `task_show(session="x", task="y")` |
| `agentwire task validate x/y` | `task_validate(session="x", task="y")` |
| `agentwire lock list` | `lock_list()` |
| `agentwire lock clean` | `lock_clean()` |
| `agentwire lock remove session` | `lock_remove(session="...")` |

### Operations (10 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire projects list` | `projects_list()` |
| `agentwire roles list` | `roles_list()` |
| `agentwire roles show name` | `role_show(name="...")` |
| `agentwire machine list` | `machines_list()` |
| `agentwire machine add id --host h --user u` | `machine_add(machine_id="...", host="...", user="...")` |
| `agentwire machine remove id` | `machine_remove(machine_id="...")` |
| `agentwire history list` | `history_list()` |
| `agentwire history show id` | `history_show(session_id="...")` |
| `agentwire history resume id -p path` | `history_resume(session_id="...", project="...")` |
| `agentwire email --body "..." --to addr` | `email_send(body="...", to="...", attachments=["..."], plain_text=False)` |

### Notifications & Network (5 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire notify event` | `session_notify(event="...")` |
| `agentwire tunnels up` | `tunnels_up()` |
| `agentwire tunnels down` | `tunnels_down()` |
| `agentwire tunnels status` | `tunnels_status()` |
| `agentwire network status` | `network_status()` |

### Status (3 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire portal status` | `portal_status()` |
| `agentwire tts status` | `tts_status()` |
| `agentwire stt status` | `stt_status()` |

### Scheduler (8 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire scheduler status` | `scheduler_status()` |
| `agentwire scheduler board` | `scheduler_board()` |
| `agentwire scheduler live --json` | `scheduler_live()` |
| `agentwire scheduler events --json` | `scheduler_events(tail=20, task="")` |
| `agentwire scheduler run task` | `scheduler_run(task="...")` |
| `agentwire scheduler report --since 8h` | `scheduler_report(since="8h", artifact=False)` |
| `agentwire scheduler enable task` | `scheduler_enable(task="...")` |
| `agentwire scheduler disable task` | `scheduler_disable(task="...")` |
| `agentwire scheduler history` | `scheduler_history(limit=20)` |

### Desktop/Portal UI (10 tools)

| Action | MCP Tool |
|--------|----------|
| List open windows | `desktop_windows_list()` |
| Open session window | `desktop_open_session(session="...", mode="monitor")` |
| Open panel | `desktop_open_panel(panel_type="sessions")` |
| Open artifact window (URL/file) | `desktop_open_artifact(url="...", title="...")` |
| Write HTML + open as artifact | `desktop_write_artifact(filename="...", html_content="...", title="...")` |
| Close window | `desktop_close_window(window_id="...")` |
| Focus window | `desktop_focus_window(window_id="...")` |
| Tile window | `desktop_tile_window(window_id="...", zone="left")` |
| Minimize all | `desktop_minimize_all()` |
| Multi-window layout | `desktop_layout(windows=[{id: "...", zone: "left"}])` |

### SDK Hierarchy (6 tools)

| Action | MCP Tool |
|--------|----------|
| Spawn child session | `sdk_child_spawn(child_name="...", parent="...", role="...", path="...", auto_kill=True)` |
| Send prompt to child | `sdk_child_send(child="...", prompt="...")` |
| Check child status | `sdk_child_status(child="...")` |
| Get child's last result | `sdk_child_result(child="...")` |
| List children | `sdk_children_list(parent="...")` |
| Kill child | `sdk_child_kill(child="...")` |

SDK hierarchy enables parent-child relationships between SDK sessions. Children notify parents on completion via `child_completed` messages. Parents auto-detect from `$TMUX_PANE` when `parent` is omitted. Killing a parent cascades to all children. Dashboard shows hierarchy tags and groups children under parents.

Portal API endpoints:
- `POST /api/session/{name}/spawn` — Spawn child (`{name, path?, type?, role?, auto_kill_on_complete?}`)
- `GET /api/session/{name}/children` — List children (`{children: [{name, busy, message_count, path}]}`)

**79 tools total.** When to use CLI vs MCP:
- **MCP tools** — Agents in sessions (orchestrators, workers)
- **CLI commands** — Humans, shell scripts, automation outside of agent sessions

**Note:** MCP tools don't support git worktree creation. Workers spawned via `pane_spawn` share the orchestrator's working directory. For isolated commits with worktrees, use the CLI `agentwire spawn --branch <name>` directly.

## Config

All in `~/.agentwire/`:

| File | Purpose |
|------|---------|
| `config.yaml` | Main config (see structure below) |
| `machines.json` | Remote machines registry |
| `scripts/` | Machine-specific helper scripts (TTS management, startup, etc.) |
| `voices/` | Custom TTS voice samples |
| `uploads/` | Uploaded images for cross-machine sharing |
| `artifacts/` | Agent-generated HTML for artifact windows |
| `logs/` | Audit logs for damage-control |

Per-session config (type, roles, voice) lives in `.agentwire.yml` in each project directory.

### Machine Scripts (`~/.agentwire/scripts/`)

Each machine has a `~/.agentwire/scripts/` directory for machine-specific helper scripts (TTS management, startup hooks, service wrappers, etc.). This is the standard location — agents should look here first and put new scripts here.

Scripts in `~/bin/` should symlink to `~/.agentwire/scripts/` so they're callable from PATH but the source of truth is in one place.

These scripts are **not** managed by agentwire — they're local to each machine and not version controlled. They exist because different machines have different roles (GPU server runs TTS, Mac runs the portal, etc.) and need different glue scripts.

### config.yaml Structure

```yaml
server:
  host: "0.0.0.0"
  port: 8765
  activity_threshold_seconds: 3  # Seconds before session considered idle
  ssl:
    cert: "~/.agentwire/cert.pem"
    key: "~/.agentwire/key.pem"

projects:
  dir: "~/projects"
  worktrees:
    enabled: true
    suffix: "-worktrees"

tts:
  backend: "runpod"  # runpod | kokoro | chatterbox | chatterbox-streaming | qwen-base-0.6b | qwen-base-1.7b | qwen-custom | qwen-design | zonos-transformer | zonos-hybrid | none
  runpod_endpoint_id: "your-endpoint-id"
  runpod_api_key: "your-api-key"
  default_voice: "dotdev"
  voices_dir: "~/.agentwire/voices"  # Custom voice samples for cloning
  exaggeration: 0.5  # Voice expressiveness (0-1, Chatterbox)
  cfg_weight: 0.5  # CFG weight (0-1, Chatterbox)
  runpod_timeout: 120  # API timeout for RunPod (seconds)

stt:
  url: "http://localhost:8101"
  timeout: 30
  backend: "auto"       # auto (moonshine → faster-whisper fallback), moonshine, whisper
  model: "base"         # Whisper model size (used when backend=whisper)
  moonshine_model: "moonshine/base"  # moonshine/tiny (faster) or moonshine/base

agent:
  command: "claude --dangerously-skip-permissions"

dev:
  source_dir: "~/projects/agentwire-dev"  # agentwire source for TTS/STT venv

services:  # Where services run (for multi-machine setups)
  portal:
    machine: null  # null = local
    port: 8765
    session_name: "agentwire-portal"  # tmux session name
  tts:
    machine: "gpu-server"  # or null for local
    port: 8100
    session_name: "agentwire-tts"
  stt:
    session_name: "agentwire-stt"
  telegram:
    machine: null
    session_name: "agentwire-telegram"

executables:  # Override executable paths (optional, auto-detected by default)
  ffmpeg: "/opt/homebrew/bin/ffmpeg"
  whisperkit-cli: "/opt/homebrew/bin/whisperkit-cli"
  hs: "/opt/homebrew/bin/hs"
  agentwire: "~/.local/bin/agentwire"

uploads:
  dir: "~/.agentwire/uploads"
  max_size_mb: 10
  cleanup_days: 7

artifacts:
  dir: "~/.agentwire/artifacts"
  max_size_mb: 10

portal:
  url: "https://localhost:8765"

notifications:
  email:
    api_key: ""  # Resend API key (or set RESEND_API_KEY env var)
    from_address: "Echo <echo@yourdomain.com>"
    default_to: "user@example.com"
    # Branding images (hosted publicly)
    banner_image_url: "https://yourdomain.com/images/banner.png"
    echo_image_url: "https://yourdomain.com/images/echo.png"
    echo_small_url: "https://yourdomain.com/images/echo-small.png"
    logo_image_url: "https://yourdomain.com/images/logo.png"

telegram:
  bot_token: ""              # from @BotFather (or TELEGRAM_BOT_TOKEN env var)
  allowed_users: []          # Telegram user IDs (integers)
  default_session: "main"   # fallback session for messages
  voice_replies: true        # convert TTS to voice notes
  forward_output: false      # continuous output forwarding
  forward_questions: true    # AskUserQuestion as inline keyboards
  forward_alerts: true       # alerts to Telegram
  notify_on:                 # extra notification triggers
    - idle
    - task_complete

scheduler:
  dispatch_cooldown: 60  # Seconds between task dispatches (default: 60)

zai:  # Z.AI API configuration (for claudeGLM sessions)
  api_key: ""  # Z.AI API key (or set ZAI_API_KEY env var)
  base_url: "https://api.z.ai/api/anthropic"
  opus_model: "glm-5"  # Model for opus tier
  sonnet_model: "glm-5"  # Model for sonnet tier
  haiku_model: "glm-4.7-flash"  # Model for haiku tier
  timeout_ms: 3000000  # API timeout (milliseconds)

session:
  default_role: "agentwire"  # Default role for new sessions
```

### .agentwire.yml (Project Config)

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
| `type` | `claude-bypass`, `claude-prompted`, `sdk-bypass`, etc. | Session permission level |
| `roles` | List of role names | Roles to load (from bundled or `~/.agentwire/roles/`) |
| `voice` | Voice name | TTS voice for this project |
| `parent` | Session name | Parent session for hierarchical notifications |
| `shell` | `/bin/sh`, `/bin/bash`, etc. | Default shell for task commands |
| `tasks` | Task definitions | Scheduled workload configurations |
| `safety` | `{allowed_paths: [...]}` | Per-project damage control allowlist |

### Task Schema

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

### Hierarchical Idle Notifications

When a session goes idle, it notifies up the hierarchy via `agentwire alert` (text-only, no audio):

```
parent session ← receives "[ALERT from child] ..."
    ↑ alert --to parent
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

### Role System

Roles define agent behavior and are composable. Mix and match roles in `.agentwire.yml` to configure orchestrators, workers, or specialized agents.

**Available roles:**

| Role | Purpose |
|------|---------|
| `agentwire` | Core session/pane/MCP tools awareness |
| `voice` | Voice communication (speak/listen) |
| `worker` | Receive tasks, execute autonomously, report back |
| `task-runner` | Scheduled task execution |
| `chatbot` | Conversational personality |
| `init` | Setup wizard behavior |

Use `agentwire roles list` to see available roles. Roles are bundled in `agentwire/roles/` and can be composed freely in `.agentwire.yml`.

## Hook Installation

Install the idle notification hook for the agentwire system:

```bash
# Create hooks directory
mkdir -p ~/.claude/hooks

# Install the hook (copies from agentwire source)
agentwire hooks install

# Verify installation
agentwire doctor
```

The hook lives at `~/.claude/hooks/idle-handler.sh` and fires on `idle_prompt` notifications.

### Queue Processor

The idle hook uses a shared queue processor for notifications:

```bash
# Install the queue processor
mkdir -p ~/.agentwire
cp ~/projects/agentwire-dev/scripts/queue-processor.sh ~/.agentwire/
chmod +x ~/.agentwire/queue-processor.sh
```

The processor sends queued alerts with 15-second gaps to prevent overwhelming orchestrators.

### Diagnosing Issues

```bash
# Check all components are installed
agentwire doctor

# View hook debug logs
tail -f /tmp/claude-hook-debug.log

# View queue processor logs
tail -f /tmp/queue-processor-debug.log
```

## claudeGLM (GLM-5 via Claude Code)

A wrapper script at `~/bin/claudeGLM` runs Claude Code against Z.AI's GLM-5 model instead of Anthropic's Claude. Same binary, same hooks, same configs — only the API endpoint and model are overridden via env vars.

```bash
claudeGLM --dangerously-skip-permissions  # interactive session
claudeGLM -p "quick task"                 # print mode
```

### How It Works

The wrapper sets env vars and calls the same `claude` binary:

| Variable | Value |
|----------|-------|
| `ANTHROPIC_BASE_URL` | `https://api.z.ai/api/anthropic` |
| `ANTHROPIC_AUTH_TOKEN` | Z.AI API key |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `glm-5` |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `glm-5` |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `glm-4.7-flash` |

Everything else is shared: `~/.claude/` config, hooks, skills, MCP servers, damage control. No file conflicts with normal Claude Code instances — env vars are process-scoped.

### Scheduler Decision

The scheduler uses **claudeGLM** (Claude Code + GLM-5) for scheduled tasks. claudeGLM provides the full Claude Code tool ecosystem (MCP, subagents, CLAUDE.md, Chrome extension) while using cost-effective Z.AI models.

### Session Type Separation

| Use Case | Agent | Config |
|----------|-------|--------|
| Human-directed work | `claude` (Anthropic) | `.agentwire.yml` → `type: claude-bypass` |
| Human-directed, cost-sensitive | `claudeGLM` (Z.AI) | Manual session creation |
| Scheduled tasks (scheduler) | `claudeGLM` (Z.AI) | `scheduler.yaml` → `type: claude-bypass` |
| SDK sessions (structured events) | Agent SDK | `type: sdk-bypass` / `sdk-prompted` / `sdk-restricted` |

By default, `agentwire new --type X` is a session-level override only and never saves to `.agentwire.yml`. Use `--persist` to opt in to saving.

### SDK Sessions (Agent SDK)

SDK sessions use the Claude Agent SDK instead of tmux. They run as pure Python async processes in the portal, providing structured JSON message events instead of terminal scraping. No tmux needed.

**Session types:**
- `sdk-bypass` → `bypassPermissions` (full automation)
- `sdk-prompted` → `default` (permission prompts)
- `sdk-restricted` → `plan` (read-only)

**Constraints:** SDK sessions live in portal memory. Portal crash = lost sessions. They require the portal to be running.

**Create via CLI:** `agentwire new -s name --type sdk-bypass -p /path/to/project`
**Create via Portal UI:** Select an `sdk-*` type in the session creation dialog

### Scheduler Task Gates

Tasks in `~/.agentwire/scheduler.yaml` can define `gate` preconditions to skip execution when changes aren't relevant:

```yaml
tasks:
  code-quality:
    gate:
      git_commit: true  # Skip if HEAD unchanged since last run
  doc-drift:
    gate:
      git_diff:         # Skip if no commits touched these paths
        - docs/
        - agentwire/
  custom-check:
    gate:
      command: "test -f /tmp/ready.flag"  # Skip if command exits non-zero
```

Gates are evaluated before dispatching and skip the task (zero AI cost) if conditions fail. Multiple gates are AND'd. Gates fail open on errors. See `docs/missions/completed/master-ralph-loop.md` for details.

### Scheduler Task Scheduling

Each task has a `schedule` field (replaces the old `interval`). The scheduler uses `_compute_next_eligible()` as the single source of truth for when a task becomes eligible.

**`schedule` field reference:**

| Key | Type | Description |
|-----|------|-------------|
| `every` | `str` | `30m`, `2h`, `1d` (duration) or `day`, `weekday`, `weekend`, `monday`..`sunday` (calendar) |
| `at` | `str` | Target time `"HH:MM"` local. Only with calendar `every` values |
| `except` | `list[str]` | Days to skip: `["saturday"]` |
| `after` | `str` | Dependency task name |
| `delay` | `str` | Wait after dependency: `"1h"`, `"30m"` |
| `cooldown` | `str` | Min time between runs: `"4h"` |
| `require_status` | `str` | `"complete"` (default) or `"any"` |
| `not_before` | `str` | Earliest time of day: `"08:00"` |
| `not_after` | `str` | Latest time of day: `"22:00"` |

Must have at least `every` OR `after` (or both).

**Examples:**
```yaml
# Duration-based interval
schedule:
  every: 4h

# Time-anchored daily
schedule:
  every: day
  at: "08:00"

# Dependency with delay
schedule:
  after: upstream-task
  delay: 1h
  cooldown: 3h

# Weekend exclusion
schedule:
  every: 4h
  except: [saturday, sunday]
```

**Restart safety:** `last_dispatch` is persisted before running. Tasks dispatched within 2h are considered "in-flight" and won't be re-dispatched after restart.

### Scheduler Task Priority

Tasks sort by `priority` first (lower = runs first), with overdue score as tiebreaker. This ensures pipeline ordering — upstream stages run before downstream when multiple tasks are overdue.

Tasks with default priority (99) sort after all prioritized tasks. Fillers always run after all regular tasks regardless of priority.

### One-Time and Limited Tasks

Tasks can auto-disable after a set number of runs:

```yaml
tasks:
  tonight-scaffold:
    # ...
    once: true        # Run once, then auto-disable (shorthand for max_runs: 1)
    schedule:
      every: 1m       # Run ASAP

  quarterly-report:
    # ...
    max_runs: 4       # Run 4 times then auto-disable
    schedule:
      every: day
      at: "09:00"
```

- `once: true` — shorthand for `max_runs: 1`
- `max_runs: N` — auto-disables task after N successful dispatches
- Scheduler logs a `task_disabled` event with `reason: max_runs_reached`
- Re-enabling a disabled task via `agentwire scheduler enable <name>` resets it

## Key Patterns

- **agentwire sessions** coordinate via voice, delegate to workers
- **worker panes** spawn within the orchestrator's session (visible dashboard)
- **Pane 0** = orchestrator, **panes 1+** = workers
- **Damage-control hooks** block dangerous ops (`rm -rf`, `git push --force`, etc.)
- **Smart TTS routing** - audio goes to browser if connected, local speakers if not

### Worker Pane Lifecycle

**Workers auto-kill after sending idle notification.** The idle hook captures output, sends alert to pane 0, then kills the worker.

Manual kill (if needed):
```bash
agentwire kill --pane 1
```

## Desktop UI Patterns

### Session Window Modes

| Mode | Element | Use Case |
|------|---------|----------|
| **Monitor** | `<pre>` with ANSI-to-HTML | Read-only output viewing, polls `tmux capture-pane` |
| **Terminal** | xterm.js | Interactive terminal, attaches via `tmux attach` |

**Important:** Monitor mode must use a simple `<pre>` element, NOT xterm.js. xterm.js requires precise container dimensions for its fit addon to work correctly. Since monitor mode just displays captured text output, a `<pre>` element with `white-space: pre-wrap` and ANSI-to-HTML conversion is simpler and more reliable.

### Artifact Windows

Agents can display HTML content in sandboxed iframe windows on the portal desktop.

**Agent workflow (MCP):**
```python
# Write HTML and open in one step
desktop_write_artifact(filename="dashboard.html", html_content="<h1>Hello</h1>", title="Dashboard")

# Or open an existing file or external URL
desktop_open_artifact(url="dashboard.html", title="Dashboard")
desktop_open_artifact(url="https://example.com", title="External")
```

**Files served from:** `~/.agentwire/artifacts/` via `/artifacts/` route.

**Sandboxing:** Local files get `allow-scripts allow-same-origin`. External URLs get `allow-scripts allow-forms allow-popups` (no same-origin).

## Docs

- CLI: `agentwire --help` or `agentwire <cmd> --help`
- `docs/PORTAL.md` - Portal modes and API reference
- `docs/security/damage-control.md` - Safety hooks documentation
- `docs/TROUBLESHOOTING.md` - Common issues and solutions
- `docs/SHELL_ESCAPING.md` - Shell escaping guide
- `docs/runpod-tts.md` - RunPod TTS setup
- `docs/tts-self-hosted.md` - Self-hosted TTS
- `docs/remote-machines.md` - Multi-machine orchestration, `~/.agentwire/machine/` agent context pattern, WSL2 setup
- `docs/remote-access.md` - Cloudflare Tunnel setup for remote access
- `docs/hammerspoon.md` - Hammerspoon push-to-talk setup for macOS
