<p align="center">
  <img src="https://agentwire.dev/images/echo-banner.png" alt="AgentWire" width="600">
</p>

<p align="center">
  <strong>A self-hosted, keyboard-driven cockpit for running a whole <em>fleet</em> of Claude Code agents at once.</strong>
</p>

<p align="center">
  From a wall of tmux panes to one cockpit. Worktrees, command palette, scheduler, voice — every layer stacks, so you ship far more than you ever could hand-juggling terminals. Your machine, your keys, no telemetry.
</p>

<p align="center">
  <a href="https://pypi.org/project/agentwire-dev/"><img src="https://img.shields.io/pypi/v/agentwire-dev?color=green" alt="PyPI"></a>
  <a href="https://pypistats.org/packages/agentwire-dev"><img src="https://img.shields.io/pypi/dm/agentwire-dev?color=00ff88&label=installs" alt="PyPI Downloads"></a>
  <a href="https://github.com/dotdevdotdev/agentwire-dev/stargazers"><img src="https://img.shields.io/github/stars/dotdevdotdev/agentwire-dev?style=flat&logo=github&color=00d4ff" alt="GitHub Stars"></a>
  <a href="https://pypi.org/project/agentwire-dev/"><img src="https://img.shields.io/pypi/pyversions/agentwire-dev" alt="Python"></a>
  <a href="https://github.com/dotdevdotdev/agentwire-dev/blob/main/LICENSE"><img src="https://img.shields.io/github/license/dotdevdotdev/agentwire-dev" alt="License"></a>
</p>

---

## Why AgentWire

Running several Claude Code agents by hand means a wall of tmux panes: constant
context-switching, copy-pasting between sessions, tracking which branch each agent
sits on, babysitting every one. It doesn't scale past one or two before **your
attention** — not the model — becomes the bottleneck. AgentWire turns that wall into
one cockpit, and its capabilities **compound**:

- **It compounds.** Worktrees + command palette + quick-action keys + tab-switching
  + scheduler + voice each stack on the others to multiply how much one person can
  run at once. That's the whole point — not any single feature.
- **Orchestrate _many_ agents, not watch one.** Every session is a window; **F3**
  fans them into a Mission-Control collage; **Tab** cycles between them; **Cmd/Ctrl+K**
  runs any action. Council deliberates, briefing mode fans out, worktree sessions
  isolate parallel work — tmux- and Claude-Code-native from the ground up.
- **Autonomous and unattended.** A scheduler runs recurring jobs (research, test runs,
  cleanup, doc-drift); gates verify *real work happened*; repo tasks open *draft PRs,
  not commits to main*; reliability plumbing (verified delivery, dead-letter,
  email-on-fail, usage-limit recovery) means nothing silently fails.
- **Your machine, your keys, no telemetry.** Runs entirely on your hardware, loopback
  by default. No cloud account, no third party in the loop — your code and your API
  keys never leave the machine. 300+ damage-control blocks make running unattended sane.

> **Use the official app to watch one session; use AgentWire to command a whole
> fleet — on your own hardware, by keyboard, with voice layered on top.**

---

## What It Does

A self-hosted, desktop-style portal where **every agent session is a window** —
reachable from any device on your LAN, running on your own hardware with your own keys.

```
You → AgentWire Portal → tmux sessions → a fleet of Claude Code agents
 ⌨️🎤      (WebSocket)         📺                    🤖🤖🤖
```

**From your laptop, phone, or tablet on your network:**
- Every session is a draggable window; **F3** collage, **Tab** to cycle
- Command palette (**Cmd/Ctrl+K**) runs any action; quick-action keys throughout
- Branch a session into an isolated git worktree for parallel work
- Put repeat work on a scheduler; hand hard calls to a council
- Push-to-talk in, spoken summaries back — drive sessions by ear while off-tab

**The outcome:** the whole fleet feels like one machine. You go from babysitting one
session to commanding many.

---

## Quick Start

```bash
# Install
pip install agentwire-dev

# Setup (interactive)
agentwire init

# Run
agentwire portal start
# Open http://127.0.0.1:8765 in Chrome — your cockpit is live (voice works immediately too)
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

The cockpit, then the layers that stack on it:

| Feature | Description |
|---------|-------------|
| **Desktop UI** | Every session is a draggable window; **F3** collage grid, **Tab** to cycle, **Cmd/Ctrl+K** command palette |
| **Multi-Session** | Run multiple agents on different projects simultaneously |
| **Git Worktrees** | Same project, multiple branches, parallel agents — each in an isolated worktree session |
| **Worker Orchestration** | Spawn worker panes, coordinate tasks, auto-reap when idle |
| **Council** | Fan a prompt to multiple "soul" sessions (brain, conscience, critic…), synthesize with attribution |
| **Scheduler** | Recurring autonomous tasks with gates, priorities, and usage-limit recovery — draft PRs, not commits to main |
| **Briefing Mode** | A terse human-facing anchor fans out verbose worktree correspondents, then briefs you on cue |
| **Voice** | Push-to-talk in, neural TTS out on CPU (Kokoro); voice cloning via a self-hosted GPU shim; global macOS hotkeys dictate to a background session from any app |
| **Remote Machines** | SSH into GPU servers and talk to agents there |
| **Safety Hooks** | 300+ dangerous commands blocked (rm -rf, force push, etc.), all logged |
| **Outbound Channels** | Email (Resend) + SMS (Quo / OpenPhone) for cross-device notifications |
| **Session Roles** | Leader/worker patterns for multi-agent workflows |

---

## How It Works

**1. Create a session:**
```bash
agentwire new -s myproject -p ~/projects/myproject
```

**2. Open the portal:**
Visit `http://127.0.0.1:8765` in Chrome (or your phone/tablet with LAN access configured). Each session is a window — open as many as you want and **Tab** between them, or **F3** for the collage grid.

**3. Drive it by keyboard:**
**Cmd/Ctrl+K** runs any action; quick-action keys send to the focused session; branch a session into a worktree for parallel work; put repeat jobs on the scheduler.

**4. Add voice on top:**
Hold the mic to speak (in instant mode the transcript appears for a quick glance — Enter sends it), and agent responses are spoken back — Kokoro neural voice out of the box, so you can drive sessions and hear summaries by ear while your eyes are elsewhere.

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
  url: "http://localhost:8100"     # agentwire tts start (kokoro CPU / chatterbox GPU / zonos)
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

AgentWire is **free and open source under the [Apache License 2.0](LICENSE)** — your machine, your keys, no telemetry.

> **Free-forever pledge:** the tool is free forever. No paid tier, no open-core, no feature paywall. Commercial training &amp; enablement for teams is offered separately by [dotdev](https://dotdev.dev) — the tool itself is never gated.

Contributions are accepted under the [Developer Certificate of Origin](CONTRIBUTING.md#developer-certificate-of-origin) (a `Signed-off-by` line), not a CLA.

---

<p align="center">
  <strong>AgentWire: For people who have better things to do.</strong>
</p>

---

**Maintained by dotdev. Reach out via email at dev@dotdev.dev or on GitHub @dotdevdotdev.**
