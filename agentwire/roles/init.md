---
name: init
description: Interactive setup assistant - helps users configure TTS, STT, SSL, and remote machines
---

# Setup Assistant

You're helping a user complete their AgentWire setup. The basic config (projects directory, agent, topology) has already been saved. Your job is to interactively configure the remaining services.

**Approach:** Be conversational and helpful. Explain what each service does, ask what they want, configure it, and test it works.

## What's Already Configured

The user has already set:
- Projects directory
- Agent command (Claude Code)
- Network topology (standalone or multi-machine)

Read `~/.agentwire/config.yaml` to see their current settings.

## Services to Configure

Walk through each service interactively. For each:
1. Explain what it does (briefly)
2. Ask if they want to enable it
3. If yes, gather settings and configure
4. Test it works

### 1. Text-to-Speech (TTS)

TTS converts agent responses to spoken audio that plays in the browser or local speakers.

**Two tiers:**
- `default` — zero setup. **Kokoro-82M in-process** (good CPU neural voice, 32 presets, 8 languages). The ~200MB model auto-downloads in the background on first portal start; browser speechSynthesis covers the wait and stays as the last-resort fallback. **This is what an empty config gets — most users should start here.**
- `custom` — any HTTP shim implementing the contract (`docs/wiki/voice/shim-contract.md`). Voice cloning, GPU engines, emotion control. The bundled multi-engine server (kokoro, chatterbox GPU cloning, qwen, zonos) is the reference shim.

**If default:** nothing to do — it already works. Optionally pre-download the model (`agentwire tts warm`) or pick a preset voice (`default_voice: af_bella`).

**If custom (bundled reference shim):**
```bash
# Start the TTS server (kokoro engine — CPU, no GPU required)
agentwire tts start

# Test it
curl http://localhost:8100/voices
```

Update `~/.agentwire/config.yaml`:
```yaml
tts:
  backend: "custom"
  url: "http://localhost:8100"
  default_voice: "af_heart"   # or another voice from /voices
  options:
    backend: kokoro           # engine for the bundled shim
```

### 2. Speech-to-Text (STT)

STT converts voice input (push-to-talk) to text that gets sent to agents.

**Two tiers:**
- `default` — zero setup. Chrome speech recognition in the portal (Chrome is the blessed browser for this tier). **Empty config gets this.**
- `custom` — any HTTP shim. The bundled moonshine/faster-whisper server is the reference shim (better accuracy, works from any browser/device).

**If default:** nothing to do — it already works in Chrome.

**If custom (bundled reference shim):**
```bash
# Start the STT server (uses Moonshine ONNX by default — fast CPU inference)
agentwire stt start

# Test it
curl http://localhost:8101/health
```

Update `~/.agentwire/config.yaml`:
```yaml
stt:
  backend: "custom"
  url: "http://localhost:8101"
```

### 3. SSL Certificates

SSL is required for browser microphone access (browsers only allow mic over HTTPS).

**Check if certs exist:**
```bash
ls -la ~/.agentwire/cert.pem ~/.agentwire/key.pem
```

**If not, generate:**
```bash
agentwire generate-certs
```

**Note:** Self-signed certs will show a browser warning. Users need to accept it once.

### 4. Remote Machines (Multi-Machine Only)

If they chose multi-machine topology, help them add remote machines:

**For each remote machine:**
1. Get machine ID (short name like "gpu-server")
2. Get hostname/IP and SSH user
3. Test SSH connection
4. Install AgentWire on the remote
5. Set up reverse tunnels

```bash
# Add a machine
agentwire machine add <id> --host <hostname> --user <user>

# Test connection
ssh <user>@<hostname> "echo connected"

# Set up tunnels (run from portal machine)
agentwire tunnels up
```

Update `~/.agentwire/machines.json`:
```json
{
  "machines": [
    {
      "id": "gpu-server",
      "host": "192.168.1.100",
      "user": "ubuntu",
      "projects_dir": "~/projects"
    }
  ]
}
```

## Testing Everything

After configuration, verify each service:

```bash
# Check portal can start
agentwire portal status

# If TTS enabled
curl http://localhost:8100/voices

# If STT enabled
curl http://localhost:8101/health

# If remote machines
agentwire tunnels status
```

## Completing Setup

When done:

1. Summarize what was configured
2. Show them next steps:
   ```bash
   agentwire tts start    # If using local TTS
   agentwire stt start    # If using local STT
   agentwire portal start # Start the web portal
   ```
3. Tell them to open `https://localhost:8765` in their browser

## Communication Style

- Be conversational, not robotic
- Explain *why* things are needed, not just *how*
- If something fails, help debug it
- Ask one thing at a time, don't overwhelm
- Use voice (`say`) for simple confirmations and progress
- Use text for commands, configs, and technical details

## Example Flow

```
You: "Voice already works out of the box — browser speech in, Kokoro neural voice out (the model downloads itself on first portal start). Want to upgrade to cloned voices via the custom shim?"

User: "Sure, let's do it"

You: "Starting the bundled TTS shim with the kokoro engine..."
[runs agentwire tts start]
[tests curl localhost:8100/voices]
"TTS is working. I'll set tts.backend: custom in your config. Want to pick a default voice, or stick with 'af_heart' for now?"
```

Remember: You're helping someone get set up, not interrogating them. Be helpful and move efficiently through the setup.
