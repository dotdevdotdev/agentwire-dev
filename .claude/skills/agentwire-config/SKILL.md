---
name: agentwire-config
description: Reference for `~/.agentwire/config.yaml` — main config structure including server/portal/SSL, projects, TTS/STT, agent, dev, services, executables, pi (binary/system_prompt/extra_env/providers), uploads/artifacts/wiki, channels (email + quo, outbound-only), scheduler, worktree, overnight, session defaults. Use when editing or debugging agentwire config, setting up TTS/STT backends, configuring pi providers, or explaining config fields to the user.
---

# AgentWire Config (`~/.agentwire/config.yaml`)

## Layout of `~/.agentwire/`

| File | Purpose |
|------|---------|
| `config.yaml` | Main config (see structure below) |
| `machines.json` | Remote machines registry |
| `scripts/` | Machine-specific helper scripts (TTS management, startup, etc.) |
| `voices/` | Custom TTS voice samples |
| `uploads/` | Uploaded images for cross-machine sharing |
| `artifacts/` | Agent-generated HTML for artifact windows |
| `wiki/` | LLM-maintained knowledge base (Karpathy LLM Wiki pattern) |
| `logs/` | Audit logs for damage-control |

Per-session config (type, roles, voice) lives in `.agentwire.yml` in each project directory (see `agentwire-project-config` skill).

## Machine Scripts (`~/.agentwire/scripts/`)

Each machine has a `~/.agentwire/scripts/` directory for machine-specific helper scripts (TTS management, startup hooks, service wrappers, etc.). This is the standard location — agents should look here first and put new scripts here.

Scripts in `~/bin/` should symlink to `~/.agentwire/scripts/` so they're callable from PATH but the source of truth is in one place.

These scripts are **not** managed by agentwire — they're local to each machine and not version controlled. They exist because different machines have different roles (GPU server runs TTS, Mac runs the portal, etc.) and need different glue scripts.

## config.yaml Structure

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
    auto_create_branch: true
    copy_files: [".env"]   # gitignored files seeded into each new worktree
                           # (git worktree add only checks out tracked files,
                           #  so .env/secrets/local config don't carry over —
                           #  add ".env.local", ".envrc", etc. as needed)

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
  custom:  # User-defined service sessions — autostart on portal launch AND
           #   `agentwire up`, health-checked by the portal watchdog, shown in
           #   the portal's Services column. Manage with `agentwire services ...`.
           #   The notifications bridge is a built-in registry entry (override
           #   by defining a service with its name).
    - name: "agent-brain"          # tmux session name (required)
      project: "~/projects/brain"  # project dir; defaults to dev source dir
      autostart: true              # boot on portal launch / `agentwire up` (default true)
      roles: "brain"               # optional; overrides project .agentwire.yml
      type: "claude-bypass"        # optional; session type override
      restart: on-failure          # never | on-failure | always (watchdog respawn
                                   #   with 30s..10m exponential backoff; default on-failure;
                                   #   `agentwire services down` always sticks)
      healthcheck:                 # optional; defaults to tmux_session/60s
        kind: tmux_session         # tmux_session | http | command
        url: "http://..."          # for http (2xx = healthy)
        command: "curl -sf ..."    # for command (exit 0 = healthy)
        interval: 60               # seconds between watchdog checks
    - "simple-service"             # string shorthand = name only, all defaults

executables:  # Override executable paths (optional, auto-detected by default)
  ffmpeg: "/opt/homebrew/bin/ffmpeg"
  whisperkit-cli: "/opt/homebrew/bin/whisperkit-cli"
  hs: "/opt/homebrew/bin/hs"
  agentwire: "~/.local/bin/agentwire"

pi:  # Pi coding agent — drives all `pi-<provider>` session types. See `agentwire-pi` skill.
  binary: "pi"  # path override if pi isn't on PATH (e.g., nvm-installed)

  # Appended via --append-system-prompt to every non-restricted pi-* session.
  # Use to teach pi about local helpers — agentwire brave, agentwire fetch, etc.
  system_prompt: |
    ## Web Search
    Use `agentwire brave "<query>"` for web search via the Brave Search API.

    ## Fetching URLs
    Use `agentwire fetch <url>` to fetch a page as clean markdown (Jina Reader).

  # Env vars injected into every pi-* session, in addition to the provider key.
  # Useful for cross-cutting tools like the Brave Search helper.
  extra_env:
    BRAVE_SEARCH_API_KEY: "BSA..."

  # Per-provider config. Session type `pi-<name>` resolves the provider here.
  # Adding a new provider is config-only — no code changes needed. For non-built-in
  # providers (e.g. DeepSeek), also register them in `~/.pi/agent/models.json`.
  providers:
    zai:
      env_var: ZAI_API_KEY
      api_key: "..."
      default_model: glm-5.1
    deepseek:
      env_var: DEEPSEEK_API_KEY
      api_key: "..."
      default_model: deepseek-chat

uploads:
  dir: "~/.agentwire/uploads"
  max_size_mb: 10
  cleanup_days: 7

artifacts:
  dir: "~/.agentwire/artifacts"
  max_size_mb: 10

wiki:
  dir: "~/.agentwire/wiki"           # Wiki vault location

portal:
  url: "https://localhost:8765"

channels:  # Outbound-only notifications. Only email + quo ship.
  email:
    api_key: ""  # Resend API key (or set RESEND_API_KEY env var)
    from_address: "Echo <echo@yourdomain.com>"
    default_to: "user@example.com"
    banner_image_url: "https://yourdomain.com/images/banner.png"
    echo_image_url: "https://yourdomain.com/images/echo.png"
    echo_small_url: "https://yourdomain.com/images/echo-small.png"
    logo_image_url: "https://yourdomain.com/images/logo.png"
  quo:
    api_key: ""              # or QUO_API_KEY / OPENPHONE_API_KEY env var
    from_number: "+1234567890"  # E.164 or phone number ID (PNxxx)
    default_to: "+0987654321"

scheduler:
  autostart: true        # Start the scheduler daemon when the portal boots (default: true)
  dispatch_cooldown: 60  # Seconds between task dispatches (default: 60)

worktree:
  worktree_dir: ~/worktrees       # Where worktrees are created
  default_base: main              # Default base branch
  default_project: ~/projects/my-repo  # Default git repo

overnight:
  window_start: "22:00"        # Start of overnight work window
  window_end: "07:00"          # End of overnight work window
  timezone: "America/Toronto"  # Empty = local timezone
  check_interval: 60           # Seconds between queue checks
  max_concurrent: 1            # Sessions to run at once
  session_timeout: 7200        # Max seconds per session (2h)
  branch_prefix: "overnight/"  # Git branch prefix
  pr_draft: true               # Create draft PRs
  session_type: "claude-auto"  # Session type for execution
  go_prompt: |                 # Prompt sent when dispatching
    You have been prepared with full context for this task.
    Begin autonomous execution now. Commit frequently.

session:
  default_role: "agentwire"  # Default role for new sessions
```
