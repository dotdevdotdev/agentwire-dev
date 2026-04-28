# Troubleshooting Guide

> Living document. Update this, don't create new versions.

Common issues and solutions for AgentWire.

---

## Quick Diagnostics

```bash
# Auto-diagnose and fix common issues
agentwire doctor

# Show what would be fixed without making changes
agentwire doctor --dry-run

# Auto-fix everything without prompts
agentwire doctor --yes

# Check network/service health
agentwire network status
```

---

## Installation Issues

### "Python 3.X.X not in '>=3.10'"

**Cause:** Python version too old.

**Fix:** Upgrade Python to 3.10+

```bash
# macOS
brew install pyenv
pyenv install 3.12.0
pyenv global 3.12.0

# Ubuntu
sudo apt install python3.12
```

### "externally-managed-environment" (Ubuntu 24.04+)

**Cause:** Ubuntu's PEP 668 protection prevents global pip installs.

**Fix:** Use a virtual environment

```bash
python3 -m venv ~/.agentwire-venv
source ~/.agentwire-venv/bin/activate
echo 'source ~/.agentwire-venv/bin/activate' >> ~/.bashrc
pip install agentwire-dev
```

### "agentwire: command not found"

**Cause:** Installation directory not in PATH.

**Fix:** Add to PATH

```bash
# Add to ~/.bashrc or ~/.zshrc
export PATH="$HOME/.local/bin:$PATH"

# Then reload
source ~/.bashrc  # or source ~/.zshrc
```

### "ffmpeg not found"

**Cause:** ffmpeg not installed (required for audio recording).

**Fix:**

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

---

## Portal Issues

### SSL Certificate Warnings in Browser

**Cause:** Self-signed certificates not trusted by browser.

**Fix:**

1. Generate certificates: `agentwire generate-certs`
2. Open https://localhost:8765 in browser
3. Click "Advanced" > "Proceed to localhost (unsafe)"
4. Browser will remember the exception

### Portal Won't Start

**Check status:**

```bash
agentwire portal status
```

**Common causes:**

| Cause | Fix |
|-------|-----|
| Port in use | `lsof -i :8765` to find process, kill it |
| Missing SSL certs | `agentwire generate-certs` |
| tmux not running | `tmux new -d -s test && tmux kill-session -t test` |

**Start with debug output:**

```bash
agentwire portal serve  # Runs in foreground with logs
```

### WebSocket Connection Failed

**Cause:** Browser blocking mixed content or SSL issues.

**Fix:**

1. Ensure using `https://` not `http://`
2. Accept the SSL certificate warning first
3. Check browser console for specific errors

---

## Voice Issues

### TTS Not Working

**Check TTS server:**

```bash
agentwire tts status
```

**If not running:**

```bash
agentwire tts start
```

**Test TTS directly:**

```bash
agentwire say "Hello world"
```

**Check configuration:**

```yaml
# ~/.agentwire/config.yaml
tts:
  backend: "runpod"  # runpod | chatterbox | none
  runpod_endpoint_id: "your-endpoint-id"
  runpod_api_key: "your-api-key"
```

See `../tts/runpod-tts.md` for RunPod setup or `../tts/tts-self-hosted.md` for self-hosting.

### STT (Speech-to-Text) Not Working

**Requirements (macOS):**

- Apple Silicon (M1/M2/M3)
- whisperkit-cli installed: `brew install whisperkit-cli`
- WhisperKit model downloaded

**Check STT server:**

```bash
agentwire stt status
```

**Test transcription manually:**

```bash
# Record a test file
ffmpeg -f avfoundation -i ":default" -t 5 -ar 16000 -ac 1 test.wav

# Transcribe
whisperkit-cli transcribe --audio-path test.wav
```

### Microphone Not Detected

**macOS:** Check System Preferences > Privacy & Security > Microphone

**Linux:** Check `arecord -l` for available devices

**Configure specific device:**

```yaml
# ~/.agentwire/config.yaml
audio:
  input_device: 0  # Device index, or "default"
```

---

## Session Issues

### Session Won't Create

**Check tmux:**

```bash
tmux list-sessions
```

**Common causes:**

| Cause | Fix |
|-------|-----|
| tmux not installed | `brew install tmux` or `apt install tmux` |
| Session name invalid | Use alphanumeric, `-`, `_` only |
| Path doesn't exist | Create directory first |

### Can't Connect to Session

**List available sessions:**

```bash
agentwire list
```

**Check session exists in tmux:**

```bash
tmux list-sessions
```

**Try attaching directly:**

```bash
tmux attach -t session-name
```

### Session Output Empty

**Capture output manually:**

```bash
tmux capture-pane -t session-name -p
```

**Check pane count:**

```bash
agentwire info -s session-name
```

---

## Remote Machine Issues

### SSH Connection Failed

**Test SSH directly:**

```bash
ssh machine-id  # Should connect without password
```

**Fix SSH key auth:**

```bash
ssh-copy-id user@host
```

**Check machine config:**

```bash
agentwire machine list
```

### Tunnel Not Working

**Check tunnel status:**

```bash
agentwire tunnels status
```

**Create tunnels:**

```bash
agentwire tunnels up
```

**Verify port is listening:**

```bash
lsof -i :8100  # Check TTS port
```

### Remote Session Timeout

**Cause:** SSH connection dropping.

**Fix:** Add to `~/.ssh/config`:

```
Host *
    ServerAliveInterval 60
    ServerAliveCountMax 3
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 600
```

---

## Safety/Hooks Issues

### Command Incorrectly Blocked

**Test command:**

```bash
agentwire safety check "your command here"
```

**View recent blocks:**

```bash
agentwire safety logs --tail 20
```

**Check pattern that matched:**

The safety check output shows which pattern blocked the command.

### Hooks Not Installed

**Check status:**

```bash
agentwire hooks status
```

**Install hooks:**

```bash
agentwire hooks install
```

---

## Idle Notification Issues

### Notifications Not Appearing

**Check hook installation:**

```bash
ls ~/.claude/hooks/idle-handler.sh
```

**Verify parent is configured:**

Check `.agentwire.yml` in the project directory:

```yaml
parent: agentwire  # Must be set for cross-session notifications
```

### Notifications Firing Too Often

**Rate limiting:** Idle notifications have a 60-second cooldown per session/pane. If a pane goes idle multiple times within 60 seconds, only the first notification fires.

**Cooldown files:**

- `/tmp/agentwire-idle/session-pane.last`

**Reset cooldown manually:**

```bash
rm -rf /tmp/agentwire-idle/
```

### Wrong Target Session

**For workers (panes 1+):** Notifications go to pane 0 automatically.

**For orchestrators (pane 0):** Notifications go to the `parent` session specified in `.agentwire.yml`.

**Check current session/pane:**

```bash
echo $AGENTWIRE_SESSION  # Current session name
echo $TMUX_PANE          # Current pane (e.g., %5)
```

### `ensure` Hangs After Summary File Appears

**Cause:** `ensure` waits for both the summary file AND the context file (`~/.agentwire/tasks/{session}.json`) to be deleted. The context file deletion happens on the hook's second idle pass, which requires another 60-second idle cycle after the summary is written.

**Fix:** Wait for the next idle cycle (~60s). Check `/tmp/claude-hook-debug.log` for `TASK: second idle` messages. If the hook isn't firing, the agent may still be processing.

**Force unblock (if stuck):**

```bash
# Check if context file still exists
ls ~/.agentwire/tasks/

# Manually delete context file (ensure will proceed)
rm ~/.agentwire/tasks/session-name.json
```

### `agentwire alert` vs `agentwire say`

| Command | Audio | Use Case |
|---------|-------|----------|
| `agentwire say` | Yes (TTS) | User-facing messages, completion announcements |
| `agentwire alert` | No (text only) | Background notifications, idle status updates |

Idle hooks use `alert` to avoid audio spam when multiple panes go idle.

---

## Performance Issues

### Slow Terminal Mode

**Cause:** WebGL not available, falling back to canvas.

**Fix:** Use a browser with WebGL support (Chrome, Firefox, Edge).

### High CPU Usage

**Check what's running:**

```bash
agentwire portal status
agentwire tts status
tmux list-sessions
```

**Kill unused sessions:**

```bash
agentwire kill -s unused-session
```

---

## Session Command Issues

### Agent Command Not Starting (Just Shows Bash Prompt)

**Symptom:** `agentwire new -s name --type claude-bypass` creates a tmux session but Claude never starts - you just see a bash prompt.

**Cause:** System prompt (from roles) contains characters that break shell escaping when sent via `tmux send-keys`.

**Common triggers:**
- Newlines in role instructions
- Unescaped quotes
- Very long command lines that wrap incorrectly

**How it manifests:**
```
% claude --append-system-prompt "line1
quote> line2"   # Bash waiting for closing quote
```

**Solution:** This was fixed by writing the system prompt to a temp file instead of embedding it in the command line. If you see this issue:

1. Make sure you're running the latest version: `agentwire rebuild`
2. Check role files for unusual characters
3. See `shell-escaping.md` for technical details

### Garbled Command Output in tmux

**Cause:** Very long commands wrap in the terminal, making the output hard to read.

**Not actually broken:** If Claude starts (check with `pgrep -f claude`), it's working fine - just display weirdness.

---

## Getting Help

1. **Run diagnostics:** `agentwire doctor`
2. **Check logs:** Portal logs are in the portal tmux session (default: `agentwire-portal`)
3. **Report issues:** https://github.com/dotdevdotdev/agentwire-dev/issues

When reporting issues, include:

- Output of `agentwire doctor --dry-run`
- Output of `agentwire --version`
- Your OS and Python version
- Steps to reproduce

## Claude Code 0-Token Action Bug (2026-01-22)

**Symptom:** Claude Code sessions get stuck with a "0 token" action - the model produces no output and the session hangs. Shows "Perambulating..." or similar thinking state indefinitely.

**Frequency:** Happens intermittently, especially:
- Voice-orchestrator sessions delegating to workers
- During Chrome browser automation (mid-interaction)
- After receiving notifications/alerts

**Observed patterns:**
- Gets stuck after tool calls complete (especially Chrome automation)
- May need multiple nudges to complete a task
- Often happens at end of workflows (after announcing completion)

**Workaround:**
- Send a follow-up prompt to nudge the session: `agentwire send -s name "continue"`
- May need to nudge 2-3 times per task
- Kill session if it's done anyway

**Status:** Suspected Claude Code bug, not an agentwire issue. Monitoring. (2026-01-22)
