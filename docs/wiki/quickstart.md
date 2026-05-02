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

---

## 2. Your first session

```bash
agentwire new -s hello -p ~/projects/hello
```

That creates:
- a tmux session named `hello`
- a Claude Code agent in pane 0 (the *orchestrator*)
- a per-project config at `~/projects/hello/.agentwire.yml` if there isn't one already

Talk to it from another terminal:

```bash
agentwire send -s hello "list the files in this directory and summarize what this project is"
agentwire output -s hello -n 50    # last 50 lines
```

Or attach directly: `tmux attach -t hello`. Both views see the same session — orchestrator, output, transcript.

To kill it: `agentwire kill -s hello`.

---

## 3. Voice in / voice out

Start the portal and the TTS server:

```bash
agentwire generate-certs              # one-time, self-signed TLS for localhost
agentwire portal start                # serves on https://localhost:8765
agentwire tts start                   # default backend (kokoro on CPU, fallback for any machine)
```

Open `https://localhost:8765` in any browser on your network — phone, tablet, laptop. Accept the self-signed cert warning. You'll see the desktop UI with a list of sessions.

To **send your voice into a session**, click the PTT button (or hold Ctrl+Space), speak, release. The transcription is dispatched to whatever session is the current target.

To **hear the agent talk back**, the agent calls `agentwire say` (it's an MCP tool: `say`). With the portal open, audio streams to your browser. With no browser connected, audio plays on the local machine's speakers. Test it:

```bash
agentwire say "Hello, this is your agent speaking."
```

For the rest of the TTS story (custom voices, alternative backends, RunPod for cloud GPUs), see [TTS](tts/tts-self-hosted.md).

---

## 4. Your first scheduled task

Define a task in `~/projects/hello/.agentwire.yml`:

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

## 5. Your first channel

The simplest bidirectional channel is **Telegram** — no app review, no domain setup, just a bot token.

1. Talk to [@BotFather](https://t.me/botfather) on Telegram, run `/newbot`, save the token it gives you.
2. Add to `~/.agentwire/config.yaml`:

   ```yaml
   channels:
     telegram:
       bot_token: "123456:ABC..."   # from BotFather
       allowed_user_ids: [12345678]  # your Telegram user ID; get it from @userinfobot
       default_session: hello
   ```

3. Start the bridge:

   ```bash
   agentwire telegram start
   ```

4. Open Telegram, find your bot by name, send it a message. It routes to the `hello` session. The session's responses come back as Telegram messages; voice is converted to audio.

Discord and Slack work the same way (composable session config — defaults + scope + per-channel overrides). → [Channels](communication/channels.md).

---

## Where to go next

Pick the next thing based on what you want to do:

- **Multi-agent work** — orchestrator/worker pattern, `pane_spawn`, role files. → [Concepts — orchestrator/worker](concepts.md#the-orchestratorworker-pattern), [CLAUDE.md](../../CLAUDE.md).
- **Run agents on a remote box** — register a machine, address sessions as `name@machine`. → [Remote machines](deployment/remote-machines.md).
- **Run programmatic DAGs** — pi or anthropic-runner workflows with retries and conditionals. → [Pi workflows](scheduling/workflows.md).
- **Lock down dangerous ops** — damage-control rules, per-project allowlists, classifier-mode auto sessions. → [Damage control](internals/damage-control.md), [claude-auto](sessions/claude-code-auto-mode.md).
- **Expose the portal to the public internet** — Cloudflare Tunnel + Zero Trust auth. → [Remote access](deployment/remote-access.md).
- **Just look up a term** — [Glossary](glossary.md).
