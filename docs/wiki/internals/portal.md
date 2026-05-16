# AgentWire Portal

> Web portal documentation. For project overview, see [CLAUDE.md](../CLAUDE.md).

## Architecture: CLI-First

The portal is a thin wrapper around CLI commands. All business logic lives in `agentwire` CLI.

### Design Principles

1. **CLI is source of truth** - session/machine logic in `__main__.py`
2. **Portal wraps CLI** - calls `run_agentwire_cmd()` instead of direct implementations
3. **JSON mode** - CLI commands support `--json` for machine-readable output
4. **WebSocket for real-time** - portal adds WebSocket layer for live updates

### How Portal Calls CLI

```python
# server.py
async def api_create_session(self, request):
    args = ["new", "-s", session_name, "--json"]
    success, result = await self.run_agentwire_cmd(args)
    return web.json_response(result)
```

### API to CLI Mapping

| API Endpoint | CLI Command |
|--------------|-------------|
| `POST /api/create` | `agentwire new -s {name}` |
| `DELETE /api/sessions/{name}` | `agentwire kill -s {name}` |
| `GET /api/sessions/local` | `agentwire list --local --sessions` |
| `GET /api/sessions/remote` | `agentwire list --remote --sessions` |
| `POST /send/{name}` | `agentwire send -s {name} {text}` |
| `POST /api/session/{name}/recreate` | `agentwire recreate -s {name}` |
| `POST /api/session/{name}/fork` | `agentwire fork -s {name}` |

### Adding New Features

1. Implement CLI command with `--json` output
2. Add portal endpoint that calls CLI via `run_agentwire_cmd()`
3. Never duplicate logic between CLI and portal

---

## Desktop UI

The portal provides an OS-like desktop interface using WinBox.js for window management. Clean desktop by default with sessions opened as draggable, resizable windows.

### Menu Bar

| Menu | Items |
|------|-------|
| **Projects** | Window listing discovered projects (folders with `.agentwire.yml`) |
| **Sessions** | Window listing all sessions with Monitor/Connect/Chat buttons |
| **⚙ (Cog)** | Dropdown with Machines and Config options |

**Chat button** appears in the Sessions window for voice-enabled sessions (`claude-*` types). Opens a voice chat window with orb visualization.

### Taskbar

The taskbar at the bottom shows open session windows and a system tray with:

| Element | Description |
|---------|-------------|
| **PTT Button** | Hold to talk to agentwire session (Ctrl+Space shortcut) |
| **Voice Indicator** | Shows agentwire session activity state |

**Voice Indicator States:**

| State | Visual | Meaning |
|-------|--------|---------|
| Idle | Gray square | No activity for `activity_threshold_seconds` (default 3s) |
| Processing | Spinning circle | Session has output activity |
| Generating | Bouncing dots | TTS generating speech |
| Playing | Audio wave bars | Audio playing in browser |

TTS states (generating/playing) take priority over processing state. Configure threshold in `config.yaml`:

```yaml
server:
  activity_threshold_seconds: 3  # Seconds before idle
```

### Projects Window

Projects are folders with `.agentwire.yml` files, discovered from `projects.dir` config. Click a project to see details and create new sessions.

| Field | Description |
|-------|-------------|
| Name | Folder name |
| Type | Session type (`claude-bypass`, `claude-prompted`, `claude-auto`, etc.) |
| Path | Full path to project folder |
| Roles | Configured roles from `.agentwire.yml` |

**Drill-down navigation:** Click a project to see details. "New Session" button opens the create session modal pre-filled with project info. Back button returns to list.

### Session Windows

Sessions can be opened from the Sessions dropdown in two modes:

| Mode | Button | Description |
|------|--------|-------------|
| **Monitor** | 👁 | Read-only output view, polls `tmux capture-pane` every 500ms |
| **Terminal** | ⌨ | Full interactive terminal via xterm.js, bidirectional |

**Window features:**
- Drag to reposition
- Resize by dragging edges/corners
- Minimize to taskbar
- Maximize / fullscreen
- Multiple windows can be open simultaneously
- Status bar shows connection state

**Monitor mode** uses a `<pre>` element with ANSI-to-HTML conversion for colored output display. Ideal for observing Claude work without needing terminal interaction.

**Terminal mode** uses xterm.js with WebGL acceleration (falls back to canvas). Full terminal emulation with vim, tab completion, readline support.

### Simultaneous Operation

Multiple windows for the same session work together:
- Monitor and Terminal windows both see the same session output
- Local `tmux attach` works alongside portal windows
- All use the same underlying tmux session

### Icons

Sessions, machines, and projects display icons in their list windows. Icons are assigned using a smart system:

**Assignment Priority:**
1. **Saved assignment** - Previously assigned icon from localStorage persists
2. **Name match** - Item name matches icon filename in `custom/` folder (e.g., session "myapp" matches `custom/myapp.png`)
3. **Random** - Unique icon from default folder, no duplicates within a list

**Folder Structure:**
```
static/icons/
├── sessions/
│   ├── custom/          # Named icons for matching
│   │   ├── agentwire.png
│   │   └── myproject.jpeg
│   ├── fox.jpeg         # Default icons for random
│   ├── robot.jpeg
│   └── ...
├── machines/
│   ├── custom/
│   └── ...
└── projects/
    ├── custom/
    └── ...
```

**Adding Custom Icons:**
1. Drop an image into `static/icons/{category}/custom/`
2. Name it to match the session/machine/project (e.g., `myproject.png` for a project named "myproject")
3. Rebuild and restart portal
4. Clear localStorage to trigger re-matching (or use the icon picker)

**Icon Picker:** Click the gear icon on any item to manually select a different icon. Manual selections are saved to localStorage.

**Name Matching Rules:**
- Case insensitive: "MyProject" matches `myproject.png`
- Ignores hyphens/special chars: "my-project" matches `myproject.png`
- Strips `@machine` suffix for remote sessions: "myapp@gpu-server" matches `myapp.png`

---

## Voice Output

Agent (or users) can trigger TTS using the unified `agentwire say` command:

```bash
agentwire say "Hello world"          # Smart routing to browser or local
agentwire say "Message" -v voice     # Specify voice
agentwire say "Message" -s session   # Specify session
```

**How it works:**

1. Command detects session from `--session`, `AGENTWIRE_SESSION` env var, or tmux session name
2. Checks if portal has active browser connections for that session
3. If connected -> Sends to portal (plays on browser/tablet)
4. If not connected -> Generates locally and plays via system audio

**Session detection priority:**
1. `--session` argument (explicit)
2. `AGENTWIRE_SESSION` env var (set automatically when session is created)
3. Current tmux session name (if running in tmux)

**For remote sessions:** `AGENTWIRE_SESSION` includes `@machine` suffix (e.g., `myproject@gpu-server`)

TTS audio includes 300ms silence padding to prevent first-syllable cutoff.

---

## AskUserQuestion Popup

When Claude Code uses the AskUserQuestion tool, the portal displays a modal with clickable options:

- Question text is spoken aloud via TTS when the popup appears
- Click any option to submit the answer
- "Type something" options show a text input with Send button
- Supports multi-line questions

---

## Session Actions

Right-click or use the actions menu on session windows for additional operations:

**For regular project sessions:**

| Action | Description |
|--------|-------------|
| New Session | Creates a sibling session in a new worktree (parallel work) |
| Fork Session | Forks Claude Code conversation context into new session |
| Recreate | Destroys session/worktree, pulls latest, creates fresh |

**Fork Session** uses Claude Code's `--resume <id> --fork-session` to create a new session that inherits the conversation context. Creates sessions named `project-fork-1`, `project-fork-2`, etc.

**For system sessions** (portal, TTS, main - names configurable via `services.*.session_name`):

| Action | Description |
|--------|-------------|
| Restart Service | Properly restarts the service |

---

## Create Session

Create new sessions from the Sessions dropdown menu:

| Field | Description |
|-------|-------------|
| Session Name | Project name (blocks `@ / \ : * ? " < > |` and spaces) |
| Machine | Local or any configured remote machine |
| Project Path | Auto-fills to `{projectsDir}/{sessionName}` |
| Voice | TTS voice for the session |

**Git Repository Detection:**
When the project path points to a git repo:
- Current branch indicator (e.g., "on main")
- **Create worktree** checkbox (checked by default)
- **Branch Name** input with auto-suggested unique name

**Session Name Derivation:**

| Machine | Worktree | CLI Session Name |
|---------|----------|------------------|
| local | no | `myapp` |
| local | yes | `myapp/jan-3-2026--1` |
| gpu-server | no | `myapp@gpu-server` |
| gpu-server | yes | `myapp/jan-3-2026--1@gpu-server` |

---

## Image Attachments

Attach images to messages for debugging, sharing screenshots, or reference:

| Method | Description |
|--------|-------------|
| Paste (Ctrl/Cmd+V) | Paste image from clipboard |
| Attach button | Click to select image file |

Images are uploaded to the configured `uploads.dir` and referenced in messages using Claude Code's `@/path/to/file` syntax. Configure in `config.yaml`:

```yaml
uploads:
  dir: "~/.agentwire/uploads"
  max_size_mb: 10
  cleanup_days: 7
```

---

## Portal API

### Core Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check |
| `/api/sessions` | GET | List all tmux sessions |
| `/api/sessions/local` | GET | List local tmux sessions |
| `/api/sessions/remote` | GET | List remote tmux sessions |
| `/api/sessions/{name}` | DELETE | Close/kill a session |
| `/api/sessions/refresh` | POST | Refresh session list from tmux |
| `/api/create` | POST | Create new session |
| `/api/check-path` | GET | Check if path exists and is git repo |
| `/api/check-branches` | GET | Get existing branches matching prefix |
| `/api/projects` | GET | List discovered projects (folders with `.agentwire.yml`) |
| `/api/projects/delete` | POST | Delete a project |
| `/api/roles` | GET | List available roles |

### Session Management

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/session/{name}/config` | POST | Update session config (voice, etc.) |
| `/api/session/{name}/recreate` | POST | Destroy and recreate session |
| `/api/session/{name}/spawn-sibling` | POST | Create parallel session in new worktree |
| `/api/session/{name}/fork` | POST | Fork Claude Code session |
| `/api/session/{name}/restart-service` | POST | Restart system service |
| `/api/sessions/{name}/connections` | GET | Get connection count for session |
### Voice & Input

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/say/{name}` | POST | Generate TTS and broadcast to session |
| `/api/local-tts/{name}` | POST | Generate TTS for local playback |
| `/api/answer/{name}` | POST | Submit answer to AskUserQuestion |
| `/api/voices` | GET | List available TTS voices |
| `/transcribe` | POST | Transcribe audio (multipart form) |
| `/upload` | POST | Upload image (multipart form) |
| `/send/{name}` | POST | Send text to session |

### Machine Management

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/machines` | GET | List configured machines |
| `/api/machines` | POST | Add a machine |
| `/api/machines/{id}` | DELETE | Remove a machine |
| `/api/machine/{id}/status` | GET | Get machine status |

### Configuration

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/config` | GET | Get current config |
| `/api/config` | POST | Save config |
| `/api/config/reload` | POST | Reload config from disk |

### Icons

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/icons/{category}` | GET | List icons for category (sessions/machines/projects). Returns `{custom: [...], default: [...]}` |

### Permission Handling

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/permission/{name}` | POST | Submit permission request (from hook) |
| `/api/permission/{name}/respond` | POST | User responds to permission request |

### History

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/history` | GET | List conversation history |
| `/api/history/{session_id}` | GET | Get session history details |
| `/api/history/{session_id}/resume` | POST | Resume a session from history (forks) |

### Desktop/Portal Control

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/desktop/windows` | GET | List open desktop windows |
| `/api/desktop/window/open` | POST | Open a window (session, panel, or artifact) |
| `/api/desktop/window/close` | POST | Close a window |
| `/api/desktop/window/focus` | POST | Bring window to front |
| `/api/desktop/window/tile` | POST | Tile window to zone (left/right/top/bottom) |
| `/api/desktop/window/minimize-all` | POST | Minimize all windows |
| `/api/desktop/layout` | POST | Apply multi-window layout |

### Artifacts

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/artifacts` | GET | List agent-generated HTML artifacts |
| `/api/artifacts/upload` | POST | Upload new artifact |
| `/api/artifacts/{filename}` | DELETE | Delete artifact file |
| `/artifacts/{filename}` | GET | Serve artifact file (static route) |

### Scheduler

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/scheduler/board` | GET | Get task board with overdue scores |
| `/api/scheduler/live` | GET | Get live scheduler state |
| `/api/scheduler/events` | GET | Get recent scheduler events |
| `/api/scheduler/output` | GET | Get scheduler session output |
| `/api/scheduler/start` | POST | Start the scheduler daemon |
| `/api/scheduler/stop` | POST | Stop the scheduler daemon |
| `/api/scheduler/tasks/{name}/run` | POST | Force-run a scheduled task |
| `/api/scheduler/tasks/{name}/enable` | POST | Enable a scheduled task |
| `/api/scheduler/tasks/{name}/disable` | POST | Disable a scheduled task |
| `/api/scheduler/tasks/{name}/events` | GET | Get events for specific task |

### Notifications

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/notify` | POST | Notify portal of session/pane state changes |

### WebSocket Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/ws/{name}` | Session WebSocket for monitor mode (JSON messages with output) |
| `/ws/terminal/{name}` | Terminal attach WebSocket (bidirectional binary data) |
