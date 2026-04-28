# Remote Machine Management

> Living document. Update this, don't create new versions.

AgentWire can manage AI agent sessions on remote machines via SSH. This guide covers adding, removing, and configuring remote machines.

---

## Adding a Machine

### CLI (Recommended)

```bash
agentwire machine add <id> --host <host> --user <user> --projects-dir <path>
```

Example:

```bash
agentwire machine add gpu-server --host 192.168.1.50 --user ubuntu --projects-dir ~/projects
```

### Portal UI

Dashboard → Machines → Add Machine

Fill in:
- **Machine ID** - Short identifier (e.g., `gpu-server`, `do-2`)
- **Host** - IP address or hostname
- **User** - SSH username
- **Projects Directory** - Where projects live on the remote machine

---

## Removing a Machine

### CLI

```bash
agentwire machine remove <id>
```

This:
- Removes from `machines.json`
- Kills active SSH tunnel
- Cleans entries from `rooms.json`
- Prints reminders for manual cleanup (SSH config, deploy keys, etc.)

### Portal UI

Dashboard → Machines → Click ✕ button on machine card

---

## Machine CLI Commands

```bash
# List all machines with connection status
agentwire machine list

# Add a machine
agentwire machine add <id> --host <host> --user <user> --projects-dir <path>

# Remove a machine (portal-side cleanup)
agentwire machine remove <id>
```

### Machine List Output

```
MACHINES:
  gpu-server: ubuntu@192.168.1.50 (~/projects) - online
  do-2: root@167.99.123.45 (~/projects) - offline
```

---

## Session Operations on Remote Machines

All session commands support the `session@machine` format:

```bash
# Create session on remote machine
agentwire new -s myproject@gpu-server

# Create worktree session on remote
agentwire new -s myproject/feature@gpu-server

# Send prompt to remote session
agentwire send -s myproject@gpu-server "run the tests"

# Read output from remote session
agentwire output -s myproject@gpu-server -n 100

# Kill remote session
agentwire kill -s myproject@gpu-server

# List all sessions (includes remote)
agentwire list
```

---

## Minimum Specs (Remote)

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| RAM | 1GB | 2GB+ |
| Storage | 10GB | 20GB+ |
| CPU | 1 vCPU | 2+ vCPU |

**Note:** The LLM runs on Anthropic's servers - remote machines only need resources for Node.js and file operations. No GPU required for Claude Code sessions (GPU only needed for TTS with Chatterbox).

---

## SSH Configuration

AgentWire uses your existing SSH configuration. Ensure you can connect:

```bash
ssh <machine-id>  # Should connect without password prompt
```

For passwordless access, add your SSH key:

```bash
ssh-copy-id user@host
```

Or add to `~/.ssh/config`:

```
Host gpu-server
    HostName 192.168.1.50
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
```

**Tip:** Enable SSH ControlMaster for faster remote operations (reuses connections instead of opening new ones each time). Add to `~/.ssh/config`:

```
Host *
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 600
```

---

## machines.json Schema

Machine configuration is stored in `~/.agentwire/machines.json`:

```json
[
  {
    "id": "gpu-server",
    "host": "192.168.1.50",
    "user": "ubuntu",
    "projects_dir": "~/projects"
  },
  {
    "id": "do-2",
    "host": "167.99.123.45",
    "user": "root",
    "projects_dir": "~/projects"
  }
]
```

| Field | Description |
|-------|-------------|
| `id` | Unique identifier, used in `session@machine` format |
| `host` | IP address or hostname |
| `user` | SSH username |
| `projects_dir` | Base directory for projects on the remote machine |

---

## Machine Context for AI Agents

### The `~/.agentwire/machine/` Pattern

Each machine should have a `~/.agentwire/machine/CLAUDE.md` — a living document describing that machine's role, services, venvs, paths, and any platform-specific gotchas.

When an AI agent needs to do ops work on a remote machine (manage services, install packages, debug the box itself), spawn a Claude Code session in `~/.agentwire/machine/` rather than SSHing and running ad-hoc commands. The agent picks up both `~/.claude/CLAUDE.md` (user preferences) and `~/.agentwire/machine/CLAUDE.md` (machine context) automatically, giving it full situational awareness without needing to rediscover everything.

```bash
# Spawn an ops session on a remote machine
ssh dotdev-pc
cd ~/.agentwire/machine
claude  # gets both global prefs and machine context

# Or spawn via agentwire from the Mac
agentwire new -s dotdev-pc-ops --machine dotdev-pc -p ~/.agentwire/machine
```

### What to Put in `~/.agentwire/machine/CLAUDE.md`

- **Machine identity** — OS, hardware specs (CPU, GPU, RAM), role in the fleet
- **Services** — what runs here, how to start/stop/check them, service file locations
- **Python venvs** — what each venv is for, how to create new ones for the platform
- **Key paths** — config files, scripts, data directories
- **Platform gotchas** — WSL paths, sudo requirements, non-standard tool locations
- **Install notes** — anything non-obvious that tripped you up (saves re-discovery)

### Example Structure

```
~/.agentwire/
├── config.yaml          # Main agentwire config
├── machines.json        # Registered remote machines
├── voices/              # TTS voice reference files
├── scripts/             # Machine-specific helper scripts
│   ├── tts              # TTS management wrapper
│   ├── tts-start        # Quick start
│   └── wsl-startup      # Boot hook (WSL example)
└── machine/
    └── CLAUDE.md        # Machine context for AI agents ← THIS
```

### Scripts in `~/.agentwire/scripts/`

Machine-specific helper scripts live here — TTS management, startup hooks, service wrappers, etc. This is the canonical location. Scripts in `~/bin/` should symlink here so they're on PATH but the source of truth stays in one place.

These scripts are not managed by agentwire and not version-controlled — they're local to each machine because different machines have different roles.

---

## WSL2 Machines

Running agentwire on Windows Subsystem for Linux has a few differences from bare Linux:

- **GPU access** — CUDA works normally; `nvidia-smi` is at `/usr/lib/wsl/lib/nvidia-smi` (not in default PATH)
- **Driver location** — GPU driver lives on the Windows host; never install Linux GPU drivers
- **CUDA toolkit** — Install `cuda-nvcc-12-4` etc. individually; the `cuda-toolkit-12-4` metapackage fails on Ubuntu 24.04 (requires `libtinfo5`, which is not available)
- **Systemd** — WSL2 supports systemd user services (`systemctl --user`); use for persistent services like TTS
- **Port exposure** — Ports are accessible from the Windows host and via SSH tunnels as normal
- **Boot hook** — WSL doesn't have a traditional init; use a startup script called from Windows Task Scheduler or Windows Terminal profile

### Recommended WSL Service Pattern

```bash
# ~/.agentwire/scripts/wsl-startup
#!/bin/bash
sudo service ssh start
systemctl --user start agentwire-tts.service
```

```ini
# ~/.config/systemd/user/agentwire-tts.service
[Unit]
Description=AgentWire TTS Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/user/projects/agentwire-dev
ExecStartPre=/bin/bash -c 'fuser -k 8100/tcp 2>/dev/null || true'
ExecStartPre=/bin/sleep 2
ExecStart=/home/user/projects/agentwire-dev/.venv-chatterbox/bin/python -m uvicorn agentwire.tts_server:app --host 0.0.0.0 --port 8100
Restart=on-failure
RestartSec=30
Environment=DEFAULT_BACKEND=chatterbox
Environment=CURRENT_VENV=chatterbox

[Install]
WantedBy=default.target
```
