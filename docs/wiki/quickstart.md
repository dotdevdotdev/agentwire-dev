# Quickstart

> Living document. Update this, don't create new versions.

A 5-minute path from `pip install` to "I have a session with voice working." For the conceptual *why*, read [Concepts](concepts.md). For the full feature set, browse the [INDEX](INDEX.md).

This page assumes you're on macOS or Linux with Python 3.10+ already installed. If anything errors, jump to [Troubleshooting](internals/troubleshooting.md).

---

## 1. Install

```bash
# macOS
brew install tmux ffmpeg
pip install agentwire-dev

# Ubuntu / Debian
sudo apt install tmux ffmpeg python3-pip python3-venv
python3 -m venv ~/.agentwire-venv && source ~/.agentwire-venv/bin/activate
pip install agentwire-dev
```

You'll also want **Claude Code** installed (`claude --version`) since the default session type runs through it.

Then install the agentwire hooks Claude Code needs to talk back to AgentWire:

```bash
agentwire hooks install
agentwire doctor      # one-shot diagnostic — confirms tmux, ffmpeg, hooks, claude
```

`agentwire doctor` reports anything missing with a fix suggestion. Re-run until it's all green.

### Recommended tmux config

AgentWire's whole UX is "tmux as the agent canvas" — but tmux's defaults fight it: no mouse scroll, a 2,000-line scrollback that loses agent transcripts, broken text selection, and a Claude Code setup tip on every session start (`focus-events off`).

The fastest fix: **`agentwire init`** offers to install the full recommended config (backing up any existing `~/.tmux.conf` first; it never clobbers without asking). The bundled template lives at `agentwire/templates/tmux.conf` and also adds a status bar, pane-split bindings, and click/drag protection so stray clicks can't poke an agent.

Already have a `~/.tmux.conf` you'd rather merge into? These are the settings that matter:

```tmux
set -g focus-events on        # Claude Code uses these; silences its per-session tip
set -g mouse on               # scroll through agent output
set -g history-limit 50000    # default 2000 is too small for agent transcripts
set -s escape-time 0          # no delay after Esc

# True color
set -g default-terminal "screen-256color"
set -ga terminal-overrides ",xterm-256color:Tc"

# Copying that works: vi-style copy mode, selection survives mouse-drag end
setw -g mode-keys vi
bind -T copy-mode-vi v send -X begin-selection
bind -T copy-mode-vi y send -X copy-selection-and-cancel
unbind -T copy-mode-vi MouseDragEnd1Pane

# Multi-client sizing — pick the policy that fits how you view sessions:
#   largest  = big clients win; small viewers see a cropped view
#   smallest = fit the smallest viewer (e.g. portal on a tablet) so the
#              window is always fully visible everywhere
#   latest   = whoever resized last wins (tmux default)
set -g window-size largest
```

`agentwire doctor` warns if the running tmux server has `focus-events` or `mouse` off.

---

## 2. Your first session

```bash
agentwire new -s hello -p ~/projects/hello
```

That creates:
- a tmux session named `hello`
- a Claude Code agent in pane 0 (the *orchestrator*)

If `~/projects/hello/.agentwire.yml` exists, its session type / roles / voice are picked up automatically. Want one written for you? Add `--persist` (e.g. `--roles agentwire,voice --persist` or `--type claude-bypass --persist`) and AgentWire saves the config — and, in a git repo, adds `.agentwire.yml` to `.gitignore`. Without `--persist`, flags are session-level overrides only. **Keep it gitignored**: it's personal config (voices, schedules, notification addresses), and a tracked copy makes worktree-dispatched runs silently use the stale committed version instead of your live edits.

Talk to it from another terminal:

```bash
agentwire send -s hello "list the files in this directory and summarize what this project is"
agentwire output -s hello -n 50    # last 50 lines
```

Or attach directly: `tmux attach -t hello`. Both views see the same session — orchestrator, output, transcript.

To kill it: `agentwire kill -s hello`.

---

## 3. Voice in / voice out

Voice works out of the box — no certs, no GPU, no commands:

```bash
agentwire portal start                # serves on http://127.0.0.1:8765
```

Open `http://127.0.0.1:8765` in **Chrome** (the blessed browser for instant
mode). To **send your voice into a session**, hold the PTT button (or
Ctrl+Space), speak, release — Chrome's built-in speech recognition
transcribes, the transcript lands in an edit-before-send bar, Enter sends it.

To **hear the agent talk back**, the agent calls the `say` MCP tool. The
default voice is **Kokoro-82M** — a genuinely good neural voice running on
CPU, identical on every OS. The first portal start downloads the model
(~200 MB, one-time) in the background with progress in the portal; until
it's ready the browser speaks via speechSynthesis (robotic but immediate),
then upgrades automatically. With no browser open, speech plays on local
speakers. Test it:

```bash
agentwire say "Hello, this is your agent speaking."
```

**Upgrading:** real cloned voices, Whisper-grade transcription, and
phone-from-anywhere come from pointing `tts:`/`stt:` at a custom shim — the
bundled servers (`agentwire tts start` / `agentwire stt start`) are reference
implementations. See the [shim contract](voice/shim-contract.md) and
[self-hosted TTS](voice/tts-self-hosted.md). Phone/LAN access additionally
needs `server.host: 0.0.0.0` + certs + the portal token (see SECURITY.md).

---

## 4. Your first scheduled task

Define a task in `~/projects/hello/.agentwire.yml` (gitignored — see §2):

```yaml
type: claude-auto      # safer than claude-bypass for unattended work
roles: [task-runner]

tasks:
  hello-task:
    prompt: |
      Say hello and tell me what files are in this directory.
      Then exit.
    idle_timeout: 30
```

Schedule it via `~/.agentwire/scheduler.yaml`:

```yaml
tasks:
  hello-nightly:
    project: ~/projects/hello
    session: hello-nightly
    task: hello-task
    schedule:
      every: day
      at: "23:00"
```

Verify it parses, then dry-run, then fire:

```bash
agentwire scheduler board                     # see the task with its schedule
agentwire scheduler run hello-nightly         # fire it now (ignores schedule)
agentwire scheduler events --task hello-nightly --tail 20    # see what happened
```

The scheduler runs the prompt headless in a fresh tmux session, captures the agent's output, writes a summary file, and tears the session down. → [Scheduled workloads](scheduling/scheduled-workloads.md) for branch management, gates, and the overnight queue.

---

## 5. Outbound notifications

Channels are outbound-only — a session sends you an email or SMS without exposing any inbound surface. Two ship today: **email** (Resend) and **quo** (OpenPhone SMS).

1. Put your Resend key in `~/.agentwire/.env` — the one place all keys live ([Secrets & API keys](security/secrets.md)):

   ```bash
   echo 'RESEND_API_KEY=re_...' >> ~/.agentwire/.env
   ```

2. Add to `~/.agentwire/config.yaml` (no key here — config never holds secrets):

   ```yaml
   channels:
     email:
       from_address: "you@example.com"   # verified Resend sender
       default_to: "you@example.com"
   ```

3. From a session, send yourself a note:

   ```bash
   agentwire email --subject "Task done" --body "Build green on main."
   ```

That's it — the session can `agentwire email ...` or `agentwire quo ...` whenever it wants to escalate. Inbound user input flows through the portal (web + tunnel), not through a chat-app bridge. → [Channels](communication/channels.md).

---

## Where to go next

Pick the next thing based on what you want to do:

- **Multi-agent work** — orchestrator/worker pattern, `pane_spawn`, role files. → [Concepts — orchestrator/worker](concepts.md#the-orchestratorworker-pattern), [CLAUDE.md](../../CLAUDE.md).
- **Run agents on a remote box** — register a machine, address sessions as `name@machine`. → [Remote machines](deployment/remote-machines.md).
- **Lock down dangerous ops** — damage-control rules, per-project allowlists, classifier-mode auto sessions. → [Damage control](internals/damage-control.md), [claude-auto](sessions/claude-code-auto-mode.md).
- **Expose the portal to the public internet** — Cloudflare Tunnel + Zero Trust auth. → [Remote access](deployment/remote-access.md).
- **Just look up a term** — [Glossary](glossary.md).
