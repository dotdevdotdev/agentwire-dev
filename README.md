<p align="center">
  <img src="https://agentwire.dev/images/echo-banner.png" alt="AgentWire" width="600">
</p>

<p align="center">
  <strong>Talk to your AI coding agents. From anywhere.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/agentwire-dev/"><img src="https://img.shields.io/pypi/v/agentwire-dev?color=green" alt="PyPI"></a>
  <a href="https://pypi.org/project/agentwire-dev/"><img src="https://img.shields.io/pypi/pyversions/agentwire-dev" alt="Python"></a>
  <a href="https://github.com/dotdevdotdev/agentwire-dev/blob/main/LICENSE"><img src="https://img.shields.io/github/license/dotdevdotdev/agentwire-dev" alt="License"></a>
  <a href="https://discord.gg/bspFZNTdUr"><img src="https://img.shields.io/discord/1234567890?color=5865F2&label=discord" alt="Discord"></a>
</p>

---

## The Problem

You're on the couch. Your AI agent is on your workstation. You have an idea.

Old way: Get up. Walk to computer. Type.

**AgentWire way:** Pull out phone. Hold button. Talk. Done.

---

## What It Does

Push-to-talk voice control for [Claude Code](https://github.com/anthropics/claude-code) or any AI coding assistant running in tmux.

<p align="center">
  <img src="https://agentwire.dev/images/demo.gif" alt="Demo" width="600">
</p>

```
Phone → AgentWire Portal → tmux session → Claude Code
 🎤        (WebSocket)         📺           🤖
```

**From your phone, tablet, or laptop on your network:**
- Hold to speak, release to send
- Watch agents work in real-time
- Hear responses via TTS
- Manage multiple projects simultaneously

---

## Quick Start

```bash
# Install
pip install agentwire-dev

# Setup (interactive)
agentwire init
agentwire generate-certs

# Run
agentwire portal start
# Open https://localhost:8765
```

**Requirements:** Python 3.10+, tmux, ffmpeg, Claude Code

<details>
<summary><strong>Platform-specific instructions</strong></summary>

**macOS:**
```bash
brew install tmux ffmpeg
pip install agentwire-dev
```

**Ubuntu/Debian:**
```bash
sudo apt install tmux ffmpeg python3-pip python3-venv
python3 -m venv ~/.agentwire-venv && source ~/.agentwire-venv/bin/activate
pip install agentwire-dev
```

**WSL2:** Same as Ubuntu. Audio is limited; use as remote worker with portal on Windows host.

</details>

---

## Features

| Feature | Description |
|---------|-------------|
| **Voice Control** | Push-to-talk from any device on your network |
| **Multi-Session** | Run multiple agents on different projects simultaneously |
| **Git Worktrees** | Same project, multiple branches, parallel agents |
| **Remote Machines** | SSH into GPU servers and talk to agents there |
| **Worker Orchestration** | Spawn worker panes, coordinate tasks, voice commands |
| **Safety Hooks** | 300+ dangerous commands blocked (rm -rf, force push, etc.) |
| **TTS Responses** | Agents talk back via browser audio |
| **SDK Sessions** | Structured Claude Agent SDK sessions with parent-child hierarchy |
| **Telegram Bridge** | Control agents from Telegram with voice notes and inline keyboards |
| **Session Roles** | Leader/worker patterns for multi-agent workflows |

---

## How It Works

**1. Create a session:**
```bash
agentwire new -s myproject -p ~/projects/myproject
```

**2. Open the portal:**
Visit `https://localhost:8765` on your phone/tablet/laptop

**3. Talk:**
Hold the mic button, speak your request, release. The transcription goes to Claude Code.

**4. Listen:**
Agent responses are spoken back via TTS (optional, requires GPU for self-hosted or RunPod).

---

## Multi-Agent Orchestration

AgentWire supports orchestrator/worker patterns for complex tasks:

```yaml
# .agentwire.yml in your project
type: claude-bypass
roles:
  - agentwire
  - voice
```

**Sessions** can spawn workers:
```bash
agentwire spawn --roles worker  # Creates a worker pane
agentwire send --pane 1 "Implement the auth module"
```

Workers execute tasks autonomously while the orchestrator coordinates.

---

## Safety

AgentWire blocks dangerous operations before they execute:

- `rm -rf /`, `git push --force`, `git reset --hard`
- Cloud CLI destructive ops (AWS, GCP, Firebase, Vercel)
- Database drops, Redis flushes, container nukes
- Sensitive file access (.env, SSH keys, credentials)

```bash
agentwire safety check "rm -rf /"
# → ✗ BLOCKED: rm with recursive or force flags

agentwire safety status
# → 312 patterns loaded, 47 blocks today
```

All decisions logged for audit trails.

---

## Voice Configuration

**TTS (Text-to-Speech):** Requires GPU. Options:

```yaml
# ~/.agentwire/config.yaml
tts:
  backend: "runpod"  # Recommended: RunPod serverless
  runpod_endpoint_id: "your-endpoint"
  runpod_api_key: "your-key"
```

Or self-host with `agentwire tts start` on a GPU machine.

**STT (Speech-to-Text):** Runs locally via `agentwire stt start`. Uses Moonshine ONNX by default (fast CPU inference, no GPU required), with automatic fallback to faster-whisper. Configure backend in `config.yaml`.

<details>
<summary><strong>Disable voice (text-only mode)</strong></summary>

```yaml
tts:
  backend: "none"
```

You can still use the portal for session management without voice.

</details>

---

## CLI Reference

<details>
<summary><strong>Session Management</strong></summary>

```bash
agentwire list                    # List sessions
agentwire new -s <name> -p <path> # Create session
agentwire kill -s <name>          # Kill session
agentwire send -s <name> "prompt" # Send to session
agentwire output -s <name>        # Read output
```

</details>

<details>
<summary><strong>Worker Panes</strong></summary>

```bash
agentwire spawn --roles worker    # Spawn worker in current session
agentwire send --pane 1 "task"    # Send to worker
agentwire output --pane 1         # Read worker output
agentwire kill --pane 1           # Kill worker
```

</details>

<details>
<summary><strong>Voice Commands</strong></summary>

```bash
agentwire say "Hello"             # TTS (auto-routes to browser)
agentwire alert "Done"            # Text notification (no audio)
agentwire listen start/stop       # Voice recording
agentwire voiceclone list         # Custom voices
```

</details>

<details>
<summary><strong>Remote Machines</strong></summary>

```bash
agentwire machine add gpu --host 10.0.0.5 --user dev
agentwire new -s ml@gpu           # Create session on remote
agentwire tunnels up              # SSH tunnels for services
```

</details>

<details>
<summary><strong>Safety & Diagnostics</strong></summary>

```bash
agentwire doctor                  # Auto-diagnose issues
agentwire safety status           # Check protection status
agentwire hooks install           # Install Claude Code hooks
agentwire network status          # Service health check
```

</details>

---

## Documentation

**Full reference:** [`docs/wiki/INDEX.md`](docs/wiki/INDEX.md)

Quick links:
- [Troubleshooting](docs/wiki/internals/troubleshooting.md)
- [Portal API](docs/wiki/internals/portal.md)
- [Remote Machines](docs/wiki/deployment/remote-machines.md)
- [RunPod TTS](docs/wiki/tts/runpod-tts.md) · [Self-Hosted TTS](docs/wiki/tts/tts-self-hosted.md)
- [Safety Hooks](docs/wiki/internals/damage-control.md)

---

## Community

- [Discord](https://discord.gg/bspFZNTdUr) - Chat, support, feature requests
- [Issues](https://github.com/dotdevdotdev/agentwire-dev/issues) - Bug reports
- [Website](https://agentwire.dev) - Docs and demos

---

## License

**Dual-licensed:**
- [AGPL v3](LICENSE) - Free for open source
- Commercial license available - [contact us](mailto:dev@dotdev.dev)

---

<p align="center">
  <strong>AgentWire: For people who have better things to do.</strong>
</p>
