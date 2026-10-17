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

# Walk ONLY the push-to-talk path (fast, no SSH waits): mic, STT shim,
# portal/tunnel, tmux+PTT — each pass/fail with a fix line when red
agentwire doctor --voice

# Check network/service health
agentwire network status
```

### Voice doesn't work / push-to-talk seems dead

`agentwire doctor --voice` walks the live voice loop end to end and tells you
which link is broken with a next step:

| Stage | Red means | Fix |
|-------|-----------|-----|
| **Mic / audio capture** | ffmpeg missing, or (macOS) no input device — mic permission revoked | Grant the mic in System Settings → Privacy → Microphone, or `brew install ffmpeg` |
| **STT process** | the Moonshine `:8101` shim (default tier) or your custom shim isn't responding | `agentwire stt start` (the portal also auto-starts the default shim) |
| **Tunnel / portal reachability** | the portal isn't up, or a required reverse tunnel is down | `agentwire portal start` / `agentwire tunnels up` |
| **tmux wiring + PTT binding** | tmux is missing (no way to deliver keystrokes); host ⌥Space binding absent (informational) | `brew install tmux`; for host PTT copy `examples/hammerspoon-ptt/init.lua` |

Break one dependency and exactly that stage goes red while the others stay
green — so the broken link is always obvious.

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
  backend: "custom"          # default = in-process Kokoro (no service to debug;
                             # check `agentwire tts status` for model state)
  url: "http://localhost:8100"
  default_voice: "default"
  options:
    backend: kokoro          # bundled-shim engine: kokoro | chatterbox |
                             # chatterbox-streaming | zonos-*
```

**Default tier (Kokoro) quirks:** the model lives in `~/.cache/kokoro_onnx/`
(`agentwire tts warm` pre-downloads it; delete the directory to force a fresh
download). If you hear the robotic browser voice, the model is still
downloading or failed — check the portal toast or `/api/voice-status`.
Python 3.14+ has no kokoro-onnx wheels yet; the portal logs a warning and
stays on speechSynthesis. If another package installed the real `phonemizer`,
it can clobber kokoro-onnx's `phonemizer-fork` (same module name) —
reinstall in a clean venv.

See `../voice/shim-contract.md` for the contract and `../voice/tts-self-hosted.md` for the full engine matrix.

### STT (Speech-to-Text) Not Working

**Default tier** (`stt.backend: default`): recognition happens in the browser.
Use Chrome (the blessed browser); check the mic permission and that the page
is on localhost or HTTPS (secure context). There is no server component.

**Custom tier** (`stt.backend: custom`): the portal uploads audio to your shim.

```bash
agentwire stt status     # probe the shim

# Test transcription manually
ffmpeg -f avfoundation -i ":default" -t 5 -ar 16000 -ac 1 test.wav
curl -s -X POST http://localhost:8101/transcribe -F file=@test.wav
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

### Hook symlinks point into a worktree or temp tree (#1118)

`agentwire doctor` prints `[!!] ... BAD LINK` and names the target when a
`~/.claude/hooks/*` symlink is dangling, outside the installed tool, or inside a
git worktree/temp tree. While that is true every session runs that tree's
un-deployed code. **Recover** with one command from the installed tool (never from
a worktree — it refuses there and says how to fix):

```bash
agentwire hooks install
```

Root cause was a unit test: the hook dirs were frozen to the real `~/.claude` at
import, so an `install_hooks(allow_foreign=True)` test repointed the real links at
its checkout. Now the dirs resolve at call time, every machine-global symlink goes
through `hooks_cli._link_into_claude` (refuses a target outside the canonical
install, before touching the existing link), and the test guard fails any test that
writes under the real `~/.claude` or `~/.agentwire`. The guard is detect-after-the-fact (an audit hook records, it does not block); prevention is the call-time dirs and `_link_into_claude`.

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

There is no cooldown/rate-limit mechanism in the current `idle-handler.sh` — every genuine idle event fires a notification. If notifications fire more often than expected, look for the agent re-entering an idle state repeatedly (e.g. a flapping tool call) rather than a rate-limiter to reset.

### Idle Notice Missing While a Background Task Runs (#1103)

A turn that ends with a background shell or monitor still running is *paused*,
not done: the harness re-wakes the session when the task reports. The idle hook
asks `agentwire idle-gate` first and **swallows** that idle (`idle-gate:
action=defer` in `/tmp/claude-hook-debug.log`); the idle that follows the last
task's report carries zero pending and sends the ordinary notice. This holds for
every consumer of idle in the hook: the parent notice, the worker-pane
summary/reap, and the scheduled-task summary/`/exit` pass. Because the cohort
ledger's `resolved_idle` placeholder is the same `kind=idle` message, a child
with a pending task no longer reads as "went idle without reporting" either.

**Signals** (`agentwire/background_tasks.py`, measured on a real session): the
pane's turn-end line `✻ Worked for 3s · done 7:37 PM · 1 shell still running`
is authoritative, read at its LIVE position only — the block directly above the
last input box (it wraps over several rows on a narrow pane; the footer
`⏵⏵ … · 1 shell` truncates and only corroborates). Older turn lines and a
footer quoted in scrollback are history and ignored. The transcript is the
fallback when the pane is unreadable: a start is a `user` row with
`toolUseResult.backgroundTaskId` (Bash) / `taskId` (Monitor), an end is a
`queue-operation` `<task-notification>` that carries a `<status>` (a Monitor's
per-event notices have none). A transcript task the pane no longer shows is
stale (from before an `/exit` + `--resume`) and counts as zero.

**The rule: unknown means HOLD, never reap.** A task-kind word the parser has
not seen next to "still running", a footer cut off (including right after the
digit: `· 1`), any unparsable digit-bearing segment, a pane with no input box
(tall draft, plan/permission/resume dialog) that still shows pending-looking
text, or a gate crash on a reap path reads as *uncertain*, which every consumer
treats as pending. Two cases stay distinct: an unparsable fragment holds, but a
clean `N <noun>` footer segment whose noun is not a task kind (`2 MCP servers
failed`, `3 files changed`) is simply not a task. A bare shell with no signal at
all behaves as before.

**A gate that gives no answer holds too.** The hook decides *in shell, before the
gate runs*, whether this idle reaches a kill (pane index > 0, or a scheduled-task
context file exists). The gate call is bounded (`AGENTWIRE_GATE_TIMEOUT`, default
8s) and counts only if it exits 0 with a parseable `defer|proceed|qualify`. Exit
1, a traceback, `kill -9`, a timeout, garbage, or a missing/old binary on a kill
path **holds** (logged, `gate_unusable` event, retried on the next idle); only a
plain pane 0 falls through to the notice. A persistently failing gate therefore
keeps worker panes alive silently, so `agentwire doctor` flags `[!!]` when the
gate failed 3+ times in the last hour.

**Declared trade-offs.** A footer-only reading (no turn-end line) with a noun off
the allowlist (`· 1 job`) reads 0/ok: the turn-end line covers the normal case.
A digit-bearing segment that is not `N noun` (`· 95%`) reads uncertain
(informational; not seen on real mode lines). A stuck monitor with a long timeout
gets ONE qualified notice to its pane-0 parent; a worker or scheduled pane stays
held until the task ends, then is reaped normally — the owner signal is `doctor`
("N task(s), Xm, qualified"). Verified on real Claude 2.1.302: Esc and Ctrl-C do
not clear "still running" (the task survives), so the pane stays truthful. The worker-pane summary/`kill --pane` and the
scheduled-task `/exit` fail to KEEP; the parent notice is held, then released
by the bound. Only "nothing readable at all" (no agent pane, no transcript)
behaves as before the gate.

**The bound releases a notice, never a kill.** After 30 minutes of deferral one
*qualified* notice is sent — `is idle, but N background task(s) still running
(waited 30m; not necessarily finished)` — to the parent (pane-0 session: by the
hook on the next idle, or by the watchdog `idle_deferred` stage if none comes;
worker pane: by the stage, to its owning pane 0). A worker pane and a scheduled
task stay held for as long as the task may be running; a live task is not ended
by a timer. `qualified` is recorded only after the enqueue succeeded (a failed
one retries next tick); a session with nobody to tell is marked `unroutable`,
never "sent". A session incarnation is the conversation id the hook supplies: a different id
restarts the clock, and a call that carries an id is NEVER restarted by a gap (so
neither a slow wake nor a restart re-arms `qualified`). Only a call with no id
falls back to a gap, and that gap is the bound itself.
Without the watchdog loaded a stuck task that never wakes its session is never
told about: `agentwire doctor` flags that as `[!!]`. State: `~/.agentwire/idle-deferred/`, events:
`~/.agentwire/idle-deferred-events.jsonl`; `agentwire doctor` lists held
notices.

### Wrong Target Session

**For workers (panes 1+):** Notifications go to pane 0 automatically.

**For orchestrators (pane 0):** Notifications go to the resolved parent: the session recorded as `created_by` at creation time, falling back to `.agentwire.yml`'s `parent:` field if unset.

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

### Text vs voice notifications

| Command | Audio | Use Case |
|---------|-------|----------|
| `agentwire say` | Yes (TTS) | User-facing messages, completion announcements |
| `agentwire msg send` | No (text only) | Polite peer report-back, dropped into the recipient's inbox |
| `agentwire notify-parent --to` | No (text only) | Worker → orchestrator status, injected as text |

Idle hooks route text alerts to the orchestrator (no audio) to avoid spam when multiple panes go idle.

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

**Symptom:** `agentwire new -s name --posture bypass` creates a tmux session but Claude never starts - you just see a bash prompt.

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

## Claude Code Sessions Hanging on a 0-Token Action

**Symptom:** Claude Code sessions occasionally get stuck on a "0 token" action — the model produces no output and the session hangs (e.g., "Perambulating..." indefinitely).

**Most common triggers:**
- Voice-orchestrator sessions delegating to workers
- Mid-interaction during Chrome browser automation
- Right after receiving notifications/alerts

**Workaround:** nudge the session with `agentwire send -s name "continue"`. May need 2–3 nudges. Kill the session if its task is otherwise complete.

This appears to be a Claude Code-side stall, not an agentwire issue.
