# Portal voice assistant (`agentwire-voice`)

One always-on Claude Code session you talk to about the whole agentwire system
("which sessions are busy", "open a quick task for the voice latency issue").
Decision record: #1197 (option 9). Build plan: #1198.

## What it is

A supervised **service of kind agent** named `agentwire-voice` — same lifecycle
as `agentwire-notifications` (watchdog restart, tmux health check), not a worker,
orchestrator or worktree session, and a root (no parent). It runs in the agentwire
source checkout (`dev.source_dir`) so `CLAUDE.md`, the skills and the agentwire MCP
tools load, with the `voice-assistant` role: ack first, act, report done or failed
out loud, spoken confirm before risky actions, never mutate git state in the
shared checkout.

It is classified as infrastructure (`services._BUILTIN_SERVICE_SESSIONS` and the
portal's Services allowlist): listed under Services and attachable
(`tmux attach -t agentwire-voice`), but out of the topology, cohorts, dangling-PR
checks and idle-nag.

**Model and effort:** Haiku 5.5 (`claude-haiku-5-5`) at `medium` effort, `low` as
the speed fallback. These are per-session `claude --model/--effort` launch flags,
recorded in the session's launch record (`~/.agentwire/sessions/agentwire-voice/metadata.json`),
so `agentwire restart` regenerates them. No other session's defaults and no user
settings change. `agentwire new --effort <level>` is the general per-session knob.

## Commands

| Command | What it does |
|---|---|
| `agentwire voice install [--effort low] [--autostart] [--no-start]` | **Human-only.** Writes the fixed `services.custom` entry into `~/.agentwire/config.yaml` and starts it. Refused without a tty, and blocked for agents by a damage-control protected-command rule (same gate as `buddy service install`). |
| `agentwire voice uninstall` | **Human-only.** Stops it and removes the entry. |
| `agentwire voice ask "<text>"` | Starts the service if needed, waits until it is ready, delivers the text. Returns after delivery; the answer is spoken. The one entry point the portal button and Hammerspoon bind to. |
| `agentwire voice status` | Health plus the model/effort the live session launched with. |
| `agentwire voice start` / `stop` | `services up` / `down` for the entry. `stop` kills the session, so the next start is a fresh conversation. |
| `agentwire voice restart` | Relaunch in place, same conversation (`agentwire restart`). |

All take `--json`.

Autostart is opt-in. With it off, `voice ask` creates the session on demand and the
watchdog leaves it alone. The entry sets `resume: true`, so a respawn (watchdog or
`voice start`) resumes the recorded conversation instead of starting fresh.

## Speech guarantee

A role prompt is not a guarantee: in the first live test (Haiku, medium) the model
answered correctly and never spoke. So the speech comes from hooks, not the model
(`agentwire/voice_hooks.py`):

| Hook | Speaks |
|---|---|
| `UserPromptSubmit` | "On it." the instant a request arrives. Not for slash commands or `[MSG from …]` deliveries. |
| `PostToolUse` on `mcp__agentwire__say` | nothing; marks that the model spoke its result this turn |
| `Stop` | if the model didn't speak: a headline of ≤15 words from `last_assistant_message`, with the full text as an on-screen card when it was cut |
| `StopFailure` | "That failed: <reason>" (usage limit, expired login, server error, ...) |
| `Notification` | approval requests (`permission_prompt`, `elicitation_dialog`): "Approval needed: … Check the portal." |

The hooks belong to the `voice-assistant` role (`roles.ROLE_SETTINGS`) and ride the
launch line as `claude --settings '<json>'`: only this process sees them, user-level
hooks (damage-control) still merge in, and `agentwire restart` regenerates them from
the recorded role. They never block a turn and fail quiet: speech is spawned
detached, so a TTS outage costs silence, never a stuck session.

**Brevity is enforced.** Any `say` to `agentwire-voice` is cut to ≤15 words at a
sentence boundary (word boundary if the first sentence is longer), and what was cut
goes on screen via `say --display`.

## One playback lane per machine

The browser plays audio through a sequential queue; the Mac speakers did not. Every
local-speaker utterance now takes a turn in one machine-wide FIFO lane
(`agentwire/playback_lane.py`, a `flock`-guarded ticket queue under
`~/.agentwire/voice/lane/`): the CLI's no-browser `say` (one turn covers every chunk)
and the portal's no-client fallback alike, across all sessions. Drop policy at
enqueue: a session's queued acks are dropped once its report, failure or approval is
queued, and when more than 20 s of audio is waiting the oldest items go first, except
the newest report/failure/approval, which always plays. A holder whose process died is
pruned, so a crashed `say` can't wedge the lane. `say --kind ack|report|failure|approval`
tells the lane what an utterance is.

## In the portal

- **Talk:** the sidebar mic button, or hold **Ctrl+Shift+Space** from anywhere (plain
  Ctrl+Space too, unless a session window has focus, since that window owns its own
  push-to-talk). Both post to `POST /api/voice/ask`, a thin wrapper over
  `agentwire voice ask`, so the button, the hotkey, Hammerspoon and a shell all
  land in the same place.
- **Hear:** `agentwire-voice` has no window, so its audio goes to the dashboard
  socket every open portal page holds, and plays through the page's one sequential
  queue. The connections check counts those pages as its listeners, so `say` stops
  falling back to the speakers while a portal page is open. With no page open it still
  plays on the speakers, through the playback lane.
- **Status chip** (next to the mic): listening → sending → working → done ✓ /
  failed ✕ / approval ?. Its state comes from the session's own speech hooks
  (`agentwire voice state <state>` → `POST /api/voice/state` → `voice_state` on the
  dashboard socket), not from guessing at output.

## Answering approvals by voice

When the assistant hits a permission dialog (e.g. a damage-control confirm), the
Notification hook speaks the **intent** in plain words, never the raw command: the
pending tool call's own `description` (Bash carries one), else the assistant's stated
plan, then "Yes or no?" (e.g. "Prune merged worktrees. Yes or no?"). The exact command
goes on screen as a toast. The intent is also saved to `~/.agentwire/voice/approval.json`.

Push-to-talk is deliberate, so your **whole utterance** is the answer. `voice ask`
sees the live dialog on the assistant's pane and makes one short model call
(`claude -p`, Haiku 5.5, low effort, no tools, 10 s timeout; ~2–5 s measured) with
the dialog's intent and everything you said. The verdict:

| Verdict | What happens |
|---|---|
| `approve` | the plain one-time **Yes** |
| `approve_and_more` | the plain Yes, then your full utterance is delivered as the next request |
| `deny` | the dialog's "No, and tell Claude what to do differently" (or plain No), then your full utterance is delivered as the feedback, so "no, do X instead" redirects |
| `other`, a timeout, an error, or anything off-script | **fail closed**: nothing is pressed, "An approval is waiting. Say yes or no." is spoken once per dialog, and your text stays queued as the next request |

Nothing you say is dropped, and nothing is approved on a guess. Voice never picks a
persistent grant ("Yes, and don't ask again", "allow all"): if a dialog has no plain
one-time Yes, it says it can't approve that one by voice and leaves it in the portal.

Queued text goes through the msg inbox as `[MSG from owner · request]`. The drain
pastes only once no dialog is live and the input box is empty, so it lands after the
dialog clears (within the watchdog's ≤60 s tick). The ack hook and the role treat
`owner` messages as the owner speaking. Keys go through `prompt_router.answer`, a
compare-and-send on the dialog's content hash, so a dialog already answered in the
portal is never answered twice.

The assistant's own spoken confirms ("That kills the scheduler session. Go ahead?")
are ordinary turns: your "yes" goes to the model as a request.

## Known gaps (tracked in #1198)

- `sessions_list` reports no busy/idle state, so "which sessions are busy" needs the
  system digest from slice 4.
- Claude Code fires the permission `Notification` a few seconds after the dialog
  appears, so the spoken approval request lags the dialog slightly.
