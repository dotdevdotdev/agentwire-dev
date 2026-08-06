# Voice Layer (EXPERIMENTAL — spike, branch-only)

> **Status: spike.** Branch `spike-voice-layer`, personal project, **not for
> merge**. It runs on the owner's own install with the owner's own API key.
> Nothing here is wired into the portal, the scheduler, or any shipped command
> path. The one hook into existing code is inert for every session that exists
> today (see [The one seam](#the-one-seam)).

A realtime voice model the owner talks to like a person — "what's the fleet
doing", "what needs me" — that is **not** a coding session. It observes and
delegates. A buddy overseeing the agents *with* the owner, not another agent in
the topology.

---

## 1. What the Realtime API actually says

This was established from the current OpenAI docs (fetched 2026-08-05) and
cross-checked against a working production implementation, not from training
data. Where the design hypothesis and the docs disagreed, the docs won.

| Question | The answer | vs. what was assumed |
|---|---|---|
| **Model id** | `gpt-realtime-2.1` (GA flagship, shipped 2026-07). Siblings: `gpt-realtime-2.1-mini`, `gpt-realtime-translate`, `gpt-live-transcribe`. | ❌ The owner called it **"gpt-voice-2"**. That name does not exist. |
| **Transport** | Three: **WebRTC** for clients that capture/play audio directly, **WebSocket** for servers already holding raw audio from a media pipeline, **SIP** for telephony. | ⚠️ Open question in the prompt. WebRTC is right here — the audio is captured in a browser, not by agentwire. |
| **Auth** | `POST /v1/realtime/client_secrets` mints a short-lived **ephemeral client secret**. Response quirk: `value` and `expires_at` are **top-level**, `session.id` is nested. | ⚠️ Unstated. Matters: it's why minting is server-side. The API key never reaches the browser. |
| **Connect** | Browser POSTs its SDP offer to `POST /v1/realtime/calls` with the client secret as bearer; gets an SDP answer back. | — |
| **Tool execution** | **Client-side.** The docs are explicit: "the client detects conversation items that contain function call arguments, it will execute custom code using those arguments." OpenAI never runs anything. | ✅ Assumption held — and it's the whole reason the security story works. |
| **Function-call events** | Tools declared as `session.tools[]`. Calls arrive on `response.done` as `output[]` items with `type: "function_call"`, carrying `name`, `arguments` (a JSON **string**), `call_id`. Result returns as `conversation.item.create` → `function_call_output`, then `response.create`. | — |
| **Turn detection** | `semantic_vad` (default) decides turns from *what was said*, not a silence timer. Or `null` for manual control. `create_response` / `interrupt_response` can be disabled independently. | ⚠️ Unstated. `semantic_vad` matters here — the owner thinking out loud about the fleet pauses mid-sentence constantly. |
| **Barge-in** | Native and free. `input_audio_buffer.speech_started` fires, the in-flight response emits `response.cancelled`. Over WebRTC the browser handles playback truncation; only WebSocket clients must hand-send `conversation.item.truncate`. | ✅ Supported — better than assumed, and needs no code on WebRTC. |
| **Audio format** | 24kHz PCM in (`{"type": "audio/pcm", "rate": 24000}`) for browser mic capture. Input transcription is a **separate** model (`gpt-4o-mini-transcribe`) from the conversational one. | ⚠️ Unstated. Two models, not one. |

**One correction worth stating plainly:** the model is `gpt-realtime-2.1`, not
"gpt-voice-2". Everything else in the prompt's architecture survived contact
with the docs.

### How the model id was actually verified — and the footgun found doing it

The claim above was re-challenged and re-verified against the live API on
2026-08-06. The method matters more than the result, because **the obvious way
to check a model id does not work**:

```
POST /v1/realtime/client_secrets   model=gpt-voice-2        → 200 OK, secret minted
POST /v1/realtime/client_secrets   model=gpt-realtime-2.1   → 200 OK, secret minted
```

**The mint endpoint does not validate the model.** It happily issues an
ephemeral secret for an id that does not exist; the model is only resolved
later, at connect time. Anyone "confirming" a model by minting a secret will
confirm a model that isn't there.

The authoritative check is the models API:

```
GET /v1/models/gpt-voice-2       → 404  "The model 'gpt-voice-2' does not exist"
GET /v1/models/gpt-realtime-2.1  → 200  id=gpt-realtime-2.1  owned_by=system
```

Corroborated by DocumentScribe's own `DEFAULT_REALTIME_MODEL`, which is the
same string. When these ids rotate — and they will — verify with
`GET /v1/models/<id>`, never with a mint.

---

## 2. The boundary: this is NOT a harness

**Write this down, because the first person to think "it could just fix that
typo itself" reintroduces the thing #730 removed.**

This repo settled on **one** coding harness: Claude Code. Every other provider
(claudeGLM, OpenCode, Agent-SDK, pi in all its flavors) was deliberately ripped
out. A voice I/O layer is not a harness, and the distinction is load-bearing:

| A harness | The voice layer |
|---|---|
| Writes code | Never writes code |
| Owns a worktree and a branch | Owns no checkout, no branch |
| Appears in the topology (orchestrator / worker / reviewer) | Does not appear in the topology at all; its role is `buddy` |
| Opens PRs, merges | Neither |
| Inherits damage-control hooks, posture, prompt routing | Has **no** such guards — which is exactly why it is read-only |

The moment it edits a file, it is a second harness, and it is an *unguarded*
one. If the buddy should change something, the answer is always **a Claude
session should do that** — and the buddy's job is to start one, not to act.

### Powerful by delegation, not by direct authority

Anything routed through a Claude session inherits damage-control hooks, worktree
isolation, posture and prompt routing. Anything the voice layer does *directly*
inherits none of it — and voice adds a failure mode the tool layer has never
had:

> **Mis-transcription.** "Kill the worker" and "kill the worktree" differ by one
> phoneme. So do most session names in a real fleet.

Hence: **read broad, write narrow.** Its power comes from spawning and directing
sessions, not from acting itself.

---

## 3. Architecture

```
   owner's voice
        │
        ▼
┌──────────────────┐   WebRTC audio + oai-events data channel
│  browser client  │◄─────────────────────────────────────────►  OpenAI Realtime
│  (client.py)     │                                              gpt-realtime-2.1
└────────┬─────────┘
         │ POST /mint    (never sees the API key)
         │ POST /tool    (function_call → result)
         ▼
┌──────────────────────────────────────────┐
│  localhost bridge  (server.py, 127.0.0.1)│
│    · mints ephemeral client secrets       │
│    · dispatches tool calls                │
└────────┬─────────────────────────────────┘
         │ allowlisted argv only
         ▼
┌──────────────────────────────────────────┐
│  agentwire CLI  (the documented SSOT)    │
│  list --sessions · worktree --list/--dangling · scheduler board · …
└──────────────────────────────────────────┘

   buddy identity: ~/.agentwire/sessions/buddy/metadata.json
   buddy inbox:    ~/.agentwire/inbox/buddy/     ──drain──▶  inbox-spool.jsonl
```

### Identity without a tmux session

The buddy registers a session **name** and a metadata record. That is all the
existing machinery needs: `msg send --to buddy`, `notify_parent`, cohort
enrollment, `wait --children` and dangling-PR detection all key off a name plus
`~/.agentwire/sessions/<name>/metadata.json` (#871's SSOT) — none of them
require tmux.

Deliberately **not** recorded, because absent keys mean *unknown* and that is
the truth:

- **No `conversation_ids`.** That chain holds Claude Code conversation UUIDs
  minted by `build_agent_command`. A synthetic id there would corrupt the one
  store that is supposed to be authoritative rather than reconstructed.
- **No `repo` / `branch` / `worktree_path`.** The buddy never works in a
  checkout.
- **No posture, no role prompt.** Those configure a Claude launch. There is no
  launch.

What *is* recorded: `kind: "voice_layer"` (so anything walking the session store
can tell at a glance this is not an agent), `role: "buddy"`, and the delivery
adapter.

### The one seam

`inbox.flush_session` assumes the recipient is a tmux session, so delivery means
pasting into pane 0. Every gate encodes that: the gone gate reads a tmux session
list, then the empty-box gate, the stuck-in-box heal, `safe_deliver`.

The buddy breaks the assumption — a real recipient whose "input box" is a live
audio conversation. Left alone the drain misreads it as a recipient that
*positively doesn't exist* (tmux is reachable, buddy isn't in the list), so
every `msg send --kind done` addressed to it dead-letters in ~5 ticks.

So there is exactly one new branch in the drain, and its **position is
load-bearing in both directions**:

```
lock → list_messages → cohort hold → ⟨ADAPTER⟩ → gone gate → box gates → safe_deliver
```

- **After the cohort hold** — a report from a child the buddy is waiting on
  belongs to `agentwire wait --children`, which reads it straight off disk.
  Spooling it first would consume it out from under that collection.
- **Before the gone gate** — that is the gate that would kill the mail.

Registration is **data, not code**: a session opts in by carrying `delivery` in
its `metadata.json`. No existing session has that key, so the branch is inert
for every session that exists today. An *unrecognized* adapter value falls
through to the ordinary tmux path rather than swallowing mail — a typo must not
become a black hole.

Delivery here means **handed to the buddy's spool** (`inbox-spool.jsonl`,
append-only), which the voice layer reads when the owner asks. It is a **pull,
not a push**: this slice never interrupts.

The read cursor stores the **last-acked message id**, not a line count. A count
is simpler and wrong: rotating or truncating the spool leaves it pointing into a
file that no longer has that shape, and the failure is silent — new mail reads
as already-seen and is never spoken. An id that is no longer present means the
spool rotated, and the safe answer is "treat everything as unread". Re-reading a
message is an annoyance; losing one is the bug.

### The tool surface is an allowlist, not a passthrough

The model chooses *which* tool. It never chooses *what runs*. Every tool builds
its own argv from validated parameters:

| Tool | Reads |
|---|---|
| `fleet_sessions` | `agentwire list --sessions` |
| `fleet_worktrees` | `agentwire worktree --list` |
| `fleet_dangling` | `agentwire worktree --dangling` |
| `fleet_scheduler` | `agentwire scheduler board` |
| `fleet_projects` | `agentwire projects list` |
| `fleet_dead_letters` | `agentwire msg dead` |
| `fleet_session_output` | `agentwire output -s <session> -n <lines>` |
| `fleet_pull_requests` | `gh pr list --repo <owner/name>` |
| `buddy_inbox` | the buddy's own spool |

A garbled session name **fails closed** and comes back as a spoken question, not
a fuzzy match. Two real injections were caught by tests while building this,
both from `-` and `.` being legal name characters:

- `--help` matched a naive pattern and reached the CLI **as a flag**.
- `../etc/passwd` matched and became a path.

Fix: every segment must start alphanumeric. Both are covered by tests.

Errors come back as **data, never exceptions** — a stalled function call leaves
the conversation hanging, whereas an error can be spoken ("I don't have a
session by that name — which one did you mean?").

---

## 4. What this slice does and does not do

**Does:**
- Buddy identity + inbox + the delivery adapter.
- Read-only fleet awareness: what is running, what is blocked, what needs you.
- Reads its own mail from other sessions.
- Speaks only when spoken to.

**Does not — and this is where the risk lives:**
- ❌ No write authority of any kind.
- ❌ No spawning or directing sessions.
- ❌ No proactive interruption.

There is deliberately **no escape hatch** for a write. Adding one means adding a
tool, in a diff someone reviews.

**Clarification, because the "does not" list reads worse than the truth:** the
voice conversation itself is complete and works end to end — mic in, voice out,
barge-in, tool calls, spoken answers. What is missing is *write authority*, not
voice. Asking "what's the fleet doing" and getting a spoken answer works today.

**Two loose ends, distinct from the deferred slices above:**

- **Not wired to a lifecycle host.** `agentwire buddy serve` is a foreground
  process started by hand. See §6 — deliberate, but unfinished.
- **`gh` is the one non-CLI dependency.** `fleet_pull_requests` shells out to
  `gh` directly because agentwire has no wrapper for it. Every other tool goes
  through the `agentwire` CLI as SSOT. If a `agentwire pr list --json` ever
  lands, this tool should move to it.

---

## 5. What was taken from DocumentScribe, and what was not

`~/projects/documentscribe` has a production voice layer over a tool layer
(persona: "Doc"). It was studied first and read-only. Its case differs in one
big way: **Doc talks to a USER about a product whose state changes only when Doc
changes it. This buddy talks to an OWNER about a live fleet of agents that are
changing underneath the conversation.**

### Taken

- **Ephemeral client secrets, server-minted.** Their `session.ts` derived the
  request body from the `openai-node` SDK source rather than prose docs — the
  same ground-truth approach that caught a dead endpoint in their live review.
  Their confirmed response shape (`value`/`expires_at` top-level, `session.id`
  nested) is reproduced here.
- **`responseActive` guard.** The server rejects a `response.create` while one
  is in flight. VAD fires its own responses, so ours must not race them.
- **Sequential tool dispatch.** One response can carry several `function_call`
  items; firing them concurrently makes their `response.create` calls race each
  other. Their `#764/#785` fix — await each in turn — is carried over verbatim
  in spirit.
- **The `<voice_mode>` addendum shape**: a base prompt plus an explicit
  addendum that overrides text-mode habits the spoken channel breaks.
- **"Say the specifics out loud."** Their hardest-won prompt lesson: "I've got
  something ready" is useless when the user isn't looking at a screen. The
  equivalent failure here is "three sessions need attention" — *which* three.
- **One tool definition set, not two.** They bridge voice into the *same* tool
  definitions the text orchestrator uses. Same instinct here: everything routes
  through the `agentwire` CLI, the documented SSOT, rather than a parallel
  implementation.

### Where it went wrong, and what that bought us

- **Prompt compliance is not a control mechanism for a specific turn.** Their
  onboarding greeting kept opening with a capability list despite the system
  prompt saying not to — and worse, the prompt fix was verified against the
  wrong model family, so it *looked* fixed. They ended up scripting the exact
  text on the `response.create` itself. Lesson taken: the persona here doesn't
  try to win that fight in prose, and the same scripted-instructions mechanism
  is available when a specific turn must say a specific thing.
- **Click-gated confirmation is unreachable hands-free.** Their nav-confirm was
  a modal a voice session had no way to click, silently blocking progress. It
  argues against ever gating a voice action behind a surface the voice channel
  can't reach — relevant the moment this slice grows write authority.
- **Stale closures in the event handler.** Their data-channel callbacks had to
  route everything through refs because the context value rebuilt on every
  render. Avoided structurally: our tool dispatch is server-side and stateless.

### Deliberately different

- **Read-only.** Doc mutates, gated by confirm tiers and an ActionCard. This
  buddy has no write path at all in this slice. Their confirm/`hard_confirm`
  tiering plus the `confirm_pending_action` meta-tool is the obvious model for
  the *next* slice — and their guardrail language against filler agreement
  ("yeah", "mmhmm" must not count as approval) is the part to copy most
  carefully.
- **A freshness rule, which has no counterpart there.** The fleet changes while
  you're talking. A fact from ninety seconds ago may already be false, so the
  persona is told to re-read rather than recall, and to date-stamp anything it
  knowingly repeats from earlier.
- **An identity rule, likewise.** Session names are long, similar, and easy to
  mishear. The persona is told never to resolve a half-heard name by picking the
  closest match — read it back and ask.
- **No wallet metering.** Theirs bills a per-response `$` wallet with
  reserve+settle. This runs on the owner's own key; cost belongs in a later
  slice if at all.
- **Stdlib-only, no framework.** Their client is React with a large provider
  tree. This is one HTML string and a `ThreadingHTTPServer`, so the spike adds
  no dependency and no packaging change.

---

## 6. Lifecycle host

The buddy is not a tmux session, so it needs somewhere to live. The
[custom-services registry](services.md) is the natural home — autostart on
portal launch, watchdog health checks, restart with backoff:

```yaml
# ~/.agentwire/config.yaml  (NOT added by this branch — the owner's call)
services:
  custom:
    - name: buddy
      command: agentwire buddy serve buddy --port 8788
      autostart: false
      healthcheck: "curl -sf http://127.0.0.1:8788/ >/dev/null"
```

Not wired up by this branch on purpose: a spike must not add itself to the
owner's startup path.

---

## 7. Running it

```bash
# one-time: give the buddy an identity
agentwire buddy register buddy

# inspect the tool surface handed to the model
agentwire buddy tools

# exercise a tool with no microphone — same dispatch path the model reaches
agentwire buddy call fleet_dangling
agentwire buddy call fleet_session_output --arg session=agentwire --arg lines=40

# other sessions can now reach it
agentwire msg send --to buddy --kind done "PR #900 is up"
agentwire buddy inbox            # read
agentwire buddy inbox --ack      # read and mark read

# talk to it (needs OPENAI_API_KEY in ~/.agentwire/.env, chmod 600)
agentwire buddy serve buddy      # → http://127.0.0.1:8788/
```

`serve` binds `127.0.0.1` only, on a port that is never a portal port
(8788 — not 8765/8100/8101), and mints a fresh bearer token per run. A
tool-execution endpoint reachable from elsewhere on the network is precisely
the unguarded surface this design exists to avoid.

---

## 8. TODO — next session picks up here

Ordered by risk, each its own reviewable diff. **Do not batch these.** The whole
point of the ordering is that each one can be rejected on its own.

### Open design questions — decide these BEFORE writing the code

These came out of reviewing the spike with the owner and are genuinely
undecided. A next session that picks an answer silently is the failure mode.

- [ ] **Q1 — Where does the confirm tier live: in the voice model, or below
      it?** *In the model* means the buddy asks "shall I spawn a worker on #884
      on branch fix-884?" and treats the reply as approval. *Below it* means the
      tool layer refuses any write that lacks a confirmation token minted by a
      previous turn, so a hallucinated or mis-transcribed approval cannot
      manufacture one. The second is more work and is the only one that holds if
      the model is wrong about what it heard. **Recommendation: below it.**
      DocumentScribe put it in the model and papered over the gaps with prompt
      language; see §5 for why that is not a control mechanism.
- [ ] **Q2 — Is "spawn a worker" a tool, or a handoff to a real session?** A
      *tool* means the voice layer builds the `worktree_create` argv itself —
      fast, but the voice layer now owns session-creation semantics, which is
      the thing §2 says it must never do. A *handoff* means the buddy composes
      the request and hands it to an actual orchestrator session, which already
      has damage-control hooks, posture and prompt routing. **Recommendation:
      handoff.** It keeps the "not a harness" boundary intact by construction
      rather than by discipline.
- [ ] **Q3 — What earns the right to interrupt?** Needed before any proactive
      speech. Candidate triggers, roughly in descending defensibility: a
      dead-lettered `done`/`escalation` (something is already lost), a dangling
      PR with no live parent (#716), a session parked on a usage limit, a
      scheduled task that failed. Everything else is noise. Also needs a
      quiet-hours answer and a "not now" that persists.

### The slices

- [ ] **T1 — Spawning.** Delegate session creation. Where the buddy becomes
      genuinely useful and where mis-transcription first has teeth. Blocked on
      Q1 and Q2. Must carry DocumentScribe's anti-filler guardrail: "yeah",
      "mmhmm", "sure" must not count as approval (§5).
- [ ] **T2 — Directing.** `msg send` to a session. Lower stakes than spawning
      (polite and non-clobbering by construction) but still a write. Blocked on
      Q1.
- [ ] **T3 — Proactive interruption.** Technically easy — the spool is already
      there — and socially the hardest to get right. An assistant that
      interrupts badly gets turned off. Blocked on Q3.
- [ ] **T4 — Lifecycle host.** Wire §6's `services.custom` entry, if and when
      the owner wants the buddy on the startup path. Independent of Q1–Q3; safe
      to do first.
- [ ] **T5 — Confirm the confirm.** Whatever Q1 decides, the confirmation path
      must be reachable **hands-free**. DocumentScribe shipped a click-gated
      confirm modal that a voice-only user could never reach (#748). If the only
      way to approve is to touch the screen, the feature does not exist.

### Standing constraints for whoever picks this up

- **This branch is not for merge.** Personal project, owner's own install,
  owner's own API key. Draft PR only; never mark ready, never merge to `main`.
- **The boundary in §2 is not negotiable.** The buddy starts and directs Claude
  sessions. It never does the work itself. Anything that reads as "it could just
  fix that typo itself" reintroduces what #730 removed.
- **Verify model ids with `GET /v1/models/<id>`**, never by minting a client
  secret — see §1.
