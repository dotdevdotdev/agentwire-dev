---
name: agentwire-config
description: Reference for `~/.agentwire/config.yaml` — main config structure including server/portal/SSL, projects, TTS/STT, agent, dev, services, executables, uploads/artifacts/wiki, channels (email + quo, outbound-only), scheduler, worktree, session defaults. Use when editing or debugging agentwire config, setting up TTS/STT backends, or explaining config fields to the user.
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

Per-session config (posture, roles, voice) lives in `.agentwire.yml` in each project directory (see `agentwire-project-config` skill).

## Machine Scripts (`~/.agentwire/scripts/`)

Each machine has a `~/.agentwire/scripts/` directory for machine-specific helper scripts (TTS management, startup hooks, service wrappers, etc.). This is the standard location — agents should look here first and put new scripts here.

Scripts in `~/bin/` should symlink to `~/.agentwire/scripts/` so they're callable from PATH but the source of truth is in one place.

These scripts are **not** managed by agentwire — they're local to each machine and not version controlled. They exist because different machines have different roles (GPU server runs TTS, Mac runs the portal, etc.) and need different glue scripts.

## config.yaml Structure

```yaml
server:
  host: "127.0.0.1"  # default; "0.0.0.0" allows LAN/phone access and requires the auth token (see SECURITY.md)
  port: 8765
  activity_threshold_seconds: 3  # Seconds before session considered idle
  ssl:
    cert: "~/.agentwire/cert.pem"
    key: "~/.agentwire/key.pem"
  # Auth token: unset = use ~/.agentwire/portal.token (auto-generated on first
  # non-loopback start; print/rotate with `agentwire portal token [--rotate]`).
  # Set a string to override the file; "" disables auth (loopback binds only —
  # the portal refuses to start on 0.0.0.0 with auth disabled).
  # auth_token: ""
  # Extra browser origins allowed on state-changing requests (exact
  # scheme://host[:port]). The portal's own origin and localhost always pass.
  # Needed when fronting with Cloudflare Tunnel:
  allowed_origins: []  # e.g. ["https://portal.example.com"]

projects:
  dir: "~/projects"
  worktrees:
    enabled: true
    suffix: "-worktrees"
    auto_create_branch: true
    copy_files: [".env", ".agentwire.yml", ".agentwire.tasks.yml"]   # gitignored files seeded into each new worktree
                           # (git worktree add only checks out tracked files,
                           #  so .env/secrets/local config don't carry over —
                           #  add ".env.local", ".envrc", etc. as needed).
                           # Keep .agentwire.yml/.agentwire.tasks.yml gitignored: a TRACKED
                           # copy means worktree runs use the committed version (HEAD) and
                           # silently ignore live edits — see agentwire-project-config skill.

tts:
  backend: "default"  # tier: default (in-process Kokoro, zero setup — ~200MB model
                      # auto-downloads on first portal start; speechSynthesis covers
                      # the wait) | custom (self-hosted shim at url)
  url: "http://localhost:8100"  # custom tier only — shim endpoint
  default_voice: "dotdev"
  voices_dir: "~/.agentwire/voices"  # Custom voice samples for cloning
  instructions: ""  # free-text prompt passed through to the shim
  options:  # opaque JSON passed to the shim; the bundled shim reads:
    backend: kokoro  # engine: kokoro | chatterbox | chatterbox-streaming | zonos-transformer | zonos-hybrid
  exaggeration: 0.5  # Voice expressiveness (0-1, Chatterbox)
  cfg_weight: 0.5  # CFG weight (0-1, Chatterbox)
  timeout: 60

stt:
  backend: "default"  # TIER (where transcription happens): default (portal-owned
                      # in-process Moonshine — bundled, auto-downloads on first boot,
                      # no setup; falls back to browser SpeechRecognition while it
                      # warms up or on py3.14+) | cloud (portal → hosted OpenAI-
                      # compatible transcription API, no shim daemon) | custom
                      # (self-hosted shim at url)
  engine: "auto"      # ENGINE (which model the self-hosted shim loads): auto | moonshine |
                      # whisper. Orthogonal to backend — used only by `agentwire stt start/serve`.
                      # `{backend: custom, engine: whisper}` = boot shim AND run faster-whisper.
  moonshine_model: "moonshine/base"  # moonshine engine only — ONNX model id (moonshine/tiny | moonshine/base)
  model: "base"       # whisper engine only — faster-whisper/openai-whisper model (tiny → large-v3)
  url: "http://localhost:8101"  # custom tier only — shim endpoint (also the `agentwire stt` port)
  cloud:  # cloud tier only — all fields optional, defaults shown
    base_url: "https://api.openai.com/v1"  # any OpenAI-compatible endpoint (Groq, Mistral, speaches, ...)
    model: "gpt-4o-mini-transcribe"
    api_key_env: "OPENAI_API_KEY"  # NAME of the env var holding the key — the key itself
                                   # never lives in config and never reaches the browser;
                                   # portal refuses to start if the var is unset
    language: ""  # optional ISO-639-1 hint
  timeout: 30
  silence_prepend_ms: 0  # prepend silence if your backend clips the first syllable
  instructions: ""  # free-text hint passed through to the shim
  options: {}  # opaque JSON passed to the shim (language hints, vocab biasing, ...)
  corrections: {}  # post-transcription find/replace, e.g. {"agent wire": "agentwire"}

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
      posture: "bypass"            # optional; posture override
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
  # Keys are env-only: RESEND_API_KEY / QUO_API_KEY in ~/.agentwire/.env
  # (docs/wiki/security/secrets.md) — never in config.yaml.
  email:
    from_address: "Echo <echo@yourdomain.com>"
    default_to: "user@example.com"
    banner_image_url: "https://yourdomain.com/images/banner.png"
    echo_image_url: "https://yourdomain.com/images/echo.png"
    echo_small_url: "https://yourdomain.com/images/echo-small.png"
    logo_image_url: "https://yourdomain.com/images/logo.png"
  quo:
    from_number: "+1234567890"  # E.164 or phone number ID (PNxxx)
    default_to: "+0987654321"

scheduler:
  autostart: true        # Start the scheduler daemon when the portal boots (default: true)
  dispatch_cooldown: 60  # Seconds between task dispatches (default: 60)
  dispatch_max_runtime: 14400  # Watchdog ceiling per dispatch in seconds; a hung ensure is killed and the task marked timeout (default: 4h, 0 disables)

usage_limit:             # Usage-limit recovery watchdog (docs/wiki/usage-limit-recovery.md)
  enabled: true          # Master switch for dialog detection/parking (default: true)
  exclude_sessions: []   # Session names never auto-parked (gates NEW parks only)

session_context:         # Context-bloat observability (Phase 0, observe-only — issue #442)
  warn_remaining_pct: 20 # Flag a session when its REMAINING context drops to/below this %.
                         # The Claude Code bar shows headroom, not usage, so LOW = bloated.
                         # Surfaced via `agentwire list --context` and MCP `sessions_context`.

worktree:                         # `agentwire worktree <name>` orchestration (WorktreeConfig).
                                  # Distinct from projects.worktrees above (the legacy
                                  # project/branch layout).
                                  # PRECEDENCE (#705): a project's .agentwire.yml `worktree:`
                                  # block (dir/base) overrides these for that repo; a
                                  # per-invocation --base flag beats both. Chain: flag →
                                  # project .agentwire.yml → this global block → built-ins.
  worktree_dir: ~/worktrees       # Root for worktrees, nested per project:
                                  # <worktree_dir>/<project>/<name>/ (mirrors ~/projects/)
  default_base: develop           # Base branch new worktrees fork from. OMIT to derive from
                                  # the repo's actual default branch (origin/HEAD, fallback to
                                  # current branch) — no hardcoded 'main'. --base always wins.
  default_project: ~/projects/my-repo  # Repo used when --project is omitted AND cwd isn't in a
                                  # git repo. Otherwise --project / the git root of cwd is used.
  naming: "{user}/{slug}"         # Optional branch-name template for NEW branches. Placeholders:
                                  # {name} (verbatim), {slug} (slugified), {user} (OS login).
                                  # Omit → branch == name verbatim. Only the git branch is
                                  # templated; the tmux session name stays {project}-{name}.

session:
  # No global default-role: a session's ROLE is derived from its spawn verb
  # (new → orchestrator or worker depending on branch, worktree → worker by
  # default, spawn → worker), then any --roles / .agentwire.yml roles: stack
  # on (worker) or replace (orchestrator) it. TOPOLOGY (worktree vs pane/main)
  # separately picks WHICH worker etiquette file (worker-worktree vs worker).
  # See resolve_roles.
  inject_soul: true          # Append the bundled 'soul' personality role to every human-facing
                             # session (appended last for recency weight). Headless roles
                             # (worker, task-runner, notifications) and soul/soul-* sessions
                             # are excluded automatically; per-session opt-out: --no-soul on new/dev

beta:
  # Opt-in gates for features that SHIP on main but stay off until asked for.
  # Every flag defaults to false, and "off" means ABSENT — not merely dormant:
  # a gated feature's role-prompt lines AND its MCP tool-description prose are
  # stripped before a model sees them, so a user who never enabled it pays no
  # tokens for it. Only a real YAML boolean turns one on — `voice_layer: "false"`
  # (quoted) stays OFF, deliberately. `agentwire doctor` reports each flag's
  # state either way. Config-only (no CLI verb); env override works via
  # AGENTWIRE_BETA__VOICE_LAYER=true.
  voice_layer: false         # The realtime voice buddy (`agentwire buddy`). Off: every buddy
                             # subcommand refuses, naming this key and OPENAI_API_KEY (which
                             # lives in ~/.agentwire/.env). See docs/wiki/voice-layer.md §0.
```

## Custom Command-Palette Items (`palette:`) (#676)

User-defined portal Cmd/Ctrl+K entries — personal CLI workflows reachable from the palette without editing source. Local-only host config (like `~/.agentwire/scripts/`), and execution-plane: `palette.` can never be added to the `agentwire config set` allowlist.

```yaml
palette:
  items:
    - id: quicktask                 # [A-Za-z0-9][A-Za-z0-9._-]* — unique
      label: "Quick task"           # shown in the palette
      icon: "⚡"                    # optional (default ⚡)
      keywords: "quicktask worktree"  # optional extra search terms
      run: "agentwire worktree {name} -p {project}"  # shell command template
      fields:                       # optional — opens a mini-form before running
        - { name: name,    label: "Branch/task name" }
        - { name: project, label: "Project", default: "agentwire-dev" }
```

- Every `{placeholder}` in `run` must be a declared field; field *values* are shell-quoted at run time (no injection via the form), while the template itself is trusted owner config.
- No `fields` → the item runs immediately on selection; output lands as a portal toast.
- CLI: `agentwire palette list [--json]`, `agentwire palette run <id> --field k=v` (300s timeout). Portal wraps these via `GET /api/palette` + `POST /api/palette/run`.
