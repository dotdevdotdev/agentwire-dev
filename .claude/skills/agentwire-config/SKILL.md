---
name: agentwire-config
description: Reference for `~/.agentwire/config.yaml` — main config structure including server/portal/SSL, projects, TTS/STT, agent, dev, services, executables, pi (binary/system_prompt/extra_env/providers), uploads/artifacts/wiki, channels (email/telegram/quo/sms/webhook/discord/slack), scheduler, worktree, overnight, session defaults. Use when editing or debugging agentwire config, setting up TTS/STT backends, wiring up a new channel bridge, configuring pi providers, or explaining config fields to the user.
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

channels:
  email:
    api_key: ""  # Resend API key (or set RESEND_API_KEY env var)
    from_address: "Echo <echo@yourdomain.com>"
    default_to: "user@example.com"
    banner_image_url: "https://yourdomain.com/images/banner.png"
    echo_image_url: "https://yourdomain.com/images/echo.png"
    echo_small_url: "https://yourdomain.com/images/echo-small.png"
    logo_image_url: "https://yourdomain.com/images/logo.png"
  telegram:
    bot_token: ""              # from @BotFather (or TELEGRAM_AGENTWIRE_BOT_TOKEN env var)
    allowed_users: []          # Telegram user IDs (integers)
    default_session: "main"    # fallback session for messages
    voice_replies: true        # convert TTS to voice notes
    forward_questions: true    # AskUserQuestion as inline keyboards
    forward_alerts: true       # alerts to Telegram
    session_name: "agentwire-telegram"
  quo:
    api_key: ""              # or QUO_API_KEY / OPENPHONE_API_KEY env var
    from_number: "+1234567890"  # E.164 or phone number ID (PNxxx)
    default_to: "+0987654321"
  sms:
    account_sid: ""          # or TWILIO_ACCOUNT_SID env var
    auth_token: ""           # or TWILIO_AUTH_TOKEN env var
    from_number: "+1234567890"
    default_to: "+0987654321"
  webhook:
    url: "https://hooks.example.com/agentwire"
    method: "POST"
    headers:
      Authorization: "Bearer xxx"
  discord:
    bot_token: ""            # or DISCORD_BOT_TOKEN env var
    allowed_user_ids: []     # Discord user IDs (integers)
    default_session: "main"
    voice_replies: true
    session_name: "agentwire-discord"
    # Composable session config hierarchy (platform → scope → specific):
    default_type: claude-bypass
    default_roles: [agentwire]
    default_instructions: ""      # applies to all Discord sessions
    dm_roles: [discord-dm]
    dm_instructions: ""           # applies to all Discord DMs
    channel_roles: [discord-dm]
    channel_instructions: ""      # applies to all Discord channel sessions
    channel_map:                  # per-channel overrides (append to scope)
      "1234567890":
        session: "backend"
        project: "~/projects/api"
        type: claude-auto         # override type
        roles: [python-expert]    # appended + deduped
        instructions: |
          Backend team channel. Focus on Python.
    user_map:                     # per-user DM overrides (DM scope only)
      "252979000000000000":
        roles: [admin]
        instructions: |
          Team lead — be direct and concise.
    # Self-configuration tip: add channel-admin to dm_roles (or default_roles)
    # so you can set up new channels by just DMing the bot. The agent will
    # edit this config.yaml and restart the bridge for you.
  slack:
    bot_token: ""            # xoxb-... or SLACK_BOT_TOKEN env var
    app_token: ""            # xapp-... or SLACK_APP_TOKEN env var
    allowed_user_ids: []     # Slack user IDs (strings)
    default_session: "main"
    session_name: "agentwire-slack"
    # Composable session config hierarchy (same as Discord):
    default_type: claude-bypass
    default_roles: [agentwire]
    default_instructions: ""      # applies to all Slack sessions
    dm_roles: [slack-dm]
    dm_instructions: ""           # applies to all Slack DMs
    channel_roles: [slack-dm]
    channel_instructions: ""      # applies to all Slack channel sessions
    channel_map:                  # per-channel overrides
      "C12345":
        session: "backend"
        type: claude-auto
        roles: [python-expert]
        instructions: |
          Backend team channel. Focus on Python.
    user_map:                     # per-user DM overrides (DM scope only)
      "U67890":
        roles: [admin]
        instructions: |
          Team lead — be direct and concise.

scheduler:
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
