<p align="center">
  <img src="https://agentwire.dev/images/echo-banner.png" alt="AgentWire" width="600">
</p>

<p align="center">
  <strong>Talk to your AI coding agents. From anywhere.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/agentwire-dev/"><img src="https://img.shields.io/pypi/v/agentwire-dev?color=green" alt="PyPI"></a>
  <a href="https://pypistats.org/packages/agentwire-dev"><img src="https://img.shields.io/pypi/dm/agentwire-dev?color=00ff88&label=installs" alt="PyPI Downloads"></a>
  <a href="https://github.com/dotdevdotdev/agentwire-dev/stargazers"><img src="https://img.shields.io/github/stars/dotdevdotdev/agentwire-dev?style=flat&logo=github&color=00d4ff" alt="GitHub Stars"></a>
  <a href="https://pypi.org/project/agentwire-dev/"><img src="https://img.shields.io/pypi/pyversions/agentwire-dev" alt="Python"></a>
  <a href="https://github.com/dotdevdotdev/agentwire-dev/blob/main/LICENSE"><img src="https://img.shields.io/github/license/dotdevdotdev/agentwire-dev" alt="License"></a>
</p>

---

## The Problem

You're on the couch. Your AI agent is on your workstation. You have an idea.

Old way: Get up. Walk to computer. Type.

**AgentWire way:** Pull out phone. Hold button. Talk. Done.

---

## What It Does

Push-to-talk voice control for [Claude Code](https://github.com/anthropics/claude-code) or any AI coding assistant running in tmux.

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

# Run
agentwire portal start
# Open http://127.0.0.1:8765 in Chrome — voice works immediately
```

**Requirements:** Python 3.10+, tmux, ffmpeg, Claude Code

**Honest setup time:** under a minute to a working voice portal with a genuinely good voice — Kokoro-82M runs on CPU out of the box (one-time ~200 MB model download in the background; the browser voice covers the wait). ~15 minutes for the full experience: cloned voices via a self-hosted TTS shim, Whisper-grade transcription, phone-from-anywhere (certs + token).

### Phone / LAN Access

The portal binds to loopback (`127.0.0.1`) by default. To access the portal from your phone, tablet, or other devices on your local network:

1. **Generate SSL certificates** (required for microphone access over non-loopback connections):
   ```bash
   agentwire generate-certs
   ```
2. **Enable LAN access**: set `server.host: 0.0.0.0` in `~/.agentwire/config.yaml`.
3. **Get your auth token**: non-loopback connections require a bearer token. Print it with:
   ```bash
   agentwire portal token
   ```
4. **Connect**: Open `https://<your-machine-ip>:8765` on your phone and enter the token when prompted.

Origin checks reject cross-site browser requests on every bind. Keep the portal on a trusted LAN — never port-forward it or run it on a public-facing VPS. For internet access, use Cloudflare Tunnel + Zero Trust. See [SECURITY.md](SECURITY.md) for details.

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

> **tmux config matters.** Default tmux has no mouse scroll, a tiny scrollback, and broken copy UX — see [Recommended tmux config](docs/wiki/quickstart.md#recommended-tmux-config), or let `agentwire init` install it for you.

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
| **Outbound Channels** | Email (Resend) + SMS (Quo / OpenPhone) for cross-device notifications |
| **Session Roles** | Leader/worker patterns for multi-agent workflows |

---

## How It Works

**1. Create a session:**
```bash
agentwire new -s myproject -p ~/projects/myproject
```

**2. Open the portal:**
Visit `http://127.0.0.1:8765` in Chrome (or your phone/tablet with LAN access configured)

**3. Talk:**
Hold the mic button, speak your request, release. In instant mode the transcript appears for a quick glance — Enter sends it to Claude Code.

**4. Listen:**
Agent responses are spoken back — Kokoro neural voice out of the box, or any TTS model behind a custom shim.

---

## Multi-Agent Orchestration

AgentWire supports orchestrator/worker patterns for complex tasks:

```yaml
# .agentwire.yml in your project (keep it gitignored — it's personal config,
# and tracked copies break worktree dispatch; agentwire adds it to .gitignore for you)
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

Two tiers, both sides:

**`default` (zero setup, what a fresh install gets):** Chrome speech recognition in, **Kokoro-82M out** — a genuinely good neural voice (top of the TTS Arena at 82M params), 32 preset voices across 8 languages, pure CPU, identical on every OS. The model (~200 MB) downloads in the background on first portal start; browser speechSynthesis covers speech until it's ready (and remains the last-resort fallback). No GPU, no certs, no commands.

**`custom` (bring your own model):** any HTTP shim implementing the [voice shim contract](docs/wiki/voice/shim-contract.md) — ~30 lines wraps anything (Deepgram, whisper.cpp, an expressive emotion-tag model). Voice cloning, GPU engines, emotion control live here. The bundled servers are reference shims:

```yaml
# ~/.agentwire/config.yaml
tts:
  backend: "custom"
  url: "http://localhost:8100"     # agentwire tts start (kokoro CPU / chatterbox GPU / qwen / zonos)
  options:
    backend: kokoro
stt:
  backend: "custom"
  url: "http://localhost:8101"     # agentwire stt start (moonshine ONNX, CPU)
```

Shims can declare capabilities (emotion tags, style instructions) via `GET /capabilities` — agentwire injects the shim's `tool_prompt` into the agent's `say` tooldef so agents actually use them.

<details>
<summary><strong>Prefer text-only?</strong></summary>

Instant mode already needs nothing — just don't press the mic. Agent speech
plays through the browser; mute the tab (or close it — with no browser
connected, speech plays on local speakers, which you can silence at the
system level).

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
agentwire send -s NAME "Done"     # Inject text into a session
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
- [Voice Shim Contract](docs/wiki/voice/shim-contract.md) · [Self-Hosted TTS](docs/wiki/voice/tts-self-hosted.md)
- [Safety Hooks](docs/wiki/internals/damage-control.md)

---

## Community

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
