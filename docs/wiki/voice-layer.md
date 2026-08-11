# Voice Layer (BETA — ships on main, default OFF)

> **Status: beta, opt-in** (owner ruling 2026-08-10, reversing "never merges").
> The code ships on `main`; the feature is off until you set
> **`beta.voice_layer: true`** in `~/.agentwire/config.yaml` and put
> `OPENAI_API_KEY` in `~/.agentwire/.env`. It runs on **your own** API key.
> Nothing here is wired into the portal or the scheduler. The one hook into
> existing code is inert for every session that has not opted in (see
> [The one seam](#the-one-seam)).

A realtime voice model the owner talks to like a person — "what's the fleet
doing", "what needs me" — that is **not** a coding session. It observes and
delegates. A buddy overseeing the agents *with* the owner, not another agent in
the topology.

---

## 0. Opting in

Two steps, and it needs both. The buddy CLI refuses until the first is done and
cannot mint a realtime session without the second, so a half-done setup fails
loudly at the step it is missing rather than at the microphone.

**1. Turn the flag on** — `~/.agentwire/config.yaml`:

```yaml
beta:
  voice_layer: true
```

**2. Provide the key** — `~/.agentwire/.env`, `chmod 600`, the one blessed spot
for secrets ([security/secrets.md](security/secrets.md)):

```
OPENAI_API_KEY=sk-...
```

Then:

```bash
agentwire doctor                # reports the flag, and whether the key is present
agentwire buddy register buddy  # the session identity (no tmux session)
agentwire buddy serve buddy     # the bridge, on 127.0.0.1:8788
```

`agentwire doctor` reports the flag's state either way — including "off",
because "is this costing me anything?" is a question a doctor run should
answer without you first enabling the thing. It reports only whether the key is
**present**; it never prints the key or any prefix of it.

### What "off" costs you: nothing, and that is tested

With the flag off, this feature is not merely dormant — it is **absent from the
tokens every session pays for**. There are **two** model-facing surfaces, and
the count is the interesting part: the first review of the gate found the
second one.

1. **Role prompts** — ~10 lines of voice-buddy etiquette in `agentwire.md`,
   `orchestrator.md`, `worker.md`, `worker-worktree.md`.
2. **The `msg_send` MCP tool description** — ~316 characters documenting the
   `voice` kind, which loads into **every agent session in every install**, and
   which the first pass missed while stating in its commit message that the
   role prompts were the only such surface. That claim was inherited from the
   brief rather than measured, which is exactly how a gate ends up with a hole
   in it. The blast radius was one docstring (no new MCP tools, no other
   description changed) — established by diffing the whole schema, not by
   reading the diff of the file we already knew about.

Both go through one mechanism, `agentwire/beta.py`: `<!-- beta:voice_layer -->`
markers resolved by `beta.render`, called from `roles.parse_role_file` (the one
funnel every role reader goes through, so `agentwire roles show` and a live
session launch agree) and from the `beta.gated_doc` decorator, applied *below*
`@mcp.tool()` so FastMCP registers the resolved text.

The bar is **byte-identical to `origin/main`**, and it is proved per surface
rather than per file — `tests/unit/test_beta_flag.py` diffs against snapshots of
main (`tests/fixtures/main_roles/`, `tests/fixtures/main_mcp_tools.json`), each
snapshot itself checked against `origin/main` so it cannot drift into agreeing
with the branch, with must-fail controls that go red if the gate ever stops
stripping. The MCP proof covers **all 108 tool descriptions by name**, so the
next ungated docstring trips it rather than shipping. Measured live with the
gate off, `msg_send`'s published description is 1800 characters — the same
number `origin/main` publishes.

A marker naming an unknown flag **fails closed** (the block is removed): text
that no gate can turn off is the failure this mechanism exists to prevent. What
makes that safe rather than silent is the paired audit — a test resolves every
shipped role file with all flags on and fails on any marker-shaped text left
over, which catches an unknown flag, a mistyped close tag, and the two shapes
that used to fail open (indented markers, a close tag at EOF with no trailing
newline). Only a real YAML boolean opens a gate, too: `voice_layer: "false"`
leaves it **off**, because `bool("false")` is `True` and a gate whose failure
direction is ON is the wrong shape however unlikely the input.

The gate is also cheap, which matters because it sits on paths every session
touches whether or not anyone enabled the feature: text containing no marker
never consults the config at all, and the flag set is read once per process
through a narrow read of `config.yaml` rather than a full `load_config` (which
imports the channel registry). Parsing all 24 role files costs 13.8ms against
main's 11ms — it was 242ms and 24 stderr log lines before those two fixes.

### The one thing the flag does not gate, and why

The `services.custom` entry in [§6 Lifecycle host](#6-lifecycle-host) needs no
gating: **nothing ships it.** The registry has no built-in buddy entry
(`services.registry()` adds only the notifications bridge), and `config.yaml`
is protected control plane that this feature never writes. The entry exists
only if you paste it yourself.

The ordering is worth stating, though, because the failure is confusing rather
than loud: paste that entry while the flag is off and the supervised process is
`agentwire buddy serve`, which now **refuses and exits** — so the watchdog sees
a dead tmux session and `doctor` reports the service unhealthy on every run.
Turn the flag on first. (The pasted block ships `autostart: false`, so it does
not boot on portal launch until you flip that too.)

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
| **Voices** | Ten, closed: `cedar`, `marin`, `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`. The docs single out `cedar`/`marin` as the newer, more natural pair. **Fixed per session:** "Once the model has emitted audio in a session, the `voice` cannot be modified for that session." | ⚠️ Unstated. Matters twice — see [The voice list](#the-voice-list-and-why-switching-costs-a-reconnect-1017). |
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

### The voice list, and why switching costs a reconnect (#1017)

The voices are enumerated once, in `realtime.VOICES`, and that one tuple feeds
all three surfaces the owner can meet: `--help` on `register`/`mint`/`serve`,
the picker on the buddy page, and the refusal text when a name is wrong.

```bash
agentwire buddy register buddy --voice marin   # persisted on the record
agentwire buddy serve buddy --voice ash        # explicit flag wins for this run
agentwire buddy serve buddy --voice cedarr
# → unknown realtime voice 'cedarr' — valid voices are: cedar, marin, alloy,
#   ash, ballad, coral, echo, sage, shimmer, verse (default: cedar)
```

Resolution is **explicit flag → the buddy's recorded voice → `cedar`**. Before
#1017 the middle term did not exist: `register --voice` wrote `realtime_voice`
onto the record and nothing ever read it, so the flag looked like a setting and
behaved like a comment.

**Why validate locally at all.** Not because the mint endpoint is known to
accept a bad voice — that is *not measured*. Because of the footgun one section
up: the mint endpoint is not a validator, it returns 200 for a model that does
not exist, so a successful mint is not evidence about anything it was handed.
The cost of being wrong is paid in the worst channel we have — the owner is not
looking at a screen, and "the buddy never spoke" reads identically as a dead
mic, an API outage, or a typo in a voice name. Ten closed values are short
enough to print in the error, so they are.

Every `--voice` refuses **before** the record is written, the sequence epoch is
reserved, or the API key is spent. That ordering is the point: a refusal that
already cost you something is a worse refusal.

**Switching is a reconnect, and it has to be.** The API fixes the voice once the
model has emitted audio, and the buddy greets on connect (#963) — so by the time
anyone wants a different voice, `session.update` is already closed to them.
There is no honest mid-call voice change; there is only a new session.

What #1017 buys is therefore not a new capability, it is the **cost** of the one
that existed: Ctrl-C the bridge → re-serve with a different `--voice` → reload
the page, three steps for a ten-value setting, becomes one click on a `<select>`
on the page. The picker posts the chosen voice with `/mint`, the bridge
validates it, adopts it, and persists it to the record so the next `serve` comes
back on it; the page tears the peer connection down and rebuilds it.

Four decisions inside that are deliberate and would otherwise look like bugs:

- **The switch re-greets**, against the #963 rule that a reconnect stays quiet.
  That rule is about reconnects the owner did not ask for, where a re-greet is
  noise. This one they asked for, and its *entire* observable result is how the
  buddy sounds — a silent switch is indistinguishable from a switch that did not
  happen, to someone with no screen. So it speaks, in the new voice, which is
  the answer to the question they asked.
- **The greet latch is released on BOTH branches**, live and idle. `stop()`
  deliberately leaves `greeted` set, so a release scoped to the live branch
  leaves the *ordinary* sequence silent: talk, press Stop, pick a voice, press
  Start — connects on the new voice and says nothing. Same failure, calmer
  route. The first cut of #1017 had exactly that asymmetry and a node test that
  built the state and asserted everything except the latch.
- **A voice is adopted after the mint succeeds, never before.** Adopting first
  left an upstream 500 with the bridge *and* the record moved to a voice that
  was never spoken: the page got its 502, the call never happened, and a reload
  came back showing a setting with no evidence behind it. It is still *minted*
  with, or "don't adopt" would be indistinguishable from ignoring the picker.
- **A failed persist does not fail the call.** `store_session_metadata` raises
  by design (#885); an unwritable record must cost stickiness across the next
  `serve`, never the live conversation. It is reported on the page rather than
  swallowed.

**One residual, open and named.** `speechSynthesis` utterances already handed to
the browser survive `stop()` — deliberately, since cancelling them is #950
defect 3 — so a switch made *while the fallback voice is mid-announcement*
genuinely overlaps the old robot voice with the new session's greet. The
teardown itself is clean (`stop()` is synchronous: data channel, peer
connection, mic tracks, `announcer.teardown()`, in that order, before `start()`
is awaited), and the model's own audio dies with the peer connection. It is only
the browser voice that can straddle the switch, and this feature makes that
one click reachable where it previously took three steps. Audible, not unsafe:
nothing is approved by it — the announcer is torn down, so the straddling
utterance anchors nothing.

**`register` on an existing buddy is idempotent in what it SAYS, too.** It always
was in what it wrote (merge-preserving, `created_at` kept), but it re-printed
the full blurb — inbox path, spool path, "other sessions can now reach it" —
over a one-word voice change, which reads like a second identity was created.
It now prints `updated voice → marin (was cedar)`, or `nothing to change`.
`--json` carries `created` and a `changes` map, computed against the record that
was there **before** the write.

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
| Inherits damage-control hooks, posture, prompt routing | Has **no** such guards |

The moment it edits a file, it is a second harness, and it is an *unguarded*
one. If the buddy should change something, the answer is always **a Claude
session should do that** — and the buddy's job is to ask one, never to act.

### Powerful by delegation, not by direct authority

Anything routed through a Claude session inherits damage-control hooks, worktree
isolation, posture and prompt routing. Anything the voice layer does *directly*
inherits none of it — and voice adds a failure mode the tool layer has never
had:

> **Mis-transcription.** "Kill the worker" and "kill the worktree" differ by one
> phoneme. So do most session names in a real fleet.

Hence: **read broad, write narrow.** Its power comes from asking sessions to do
things, not from acting itself.

### The write path exists now, and it is not guarded by anything but itself

Slice 1 gives the buddy exactly one write: `agentwire msg send` to a session
that is already running. Until it landed, "has no such guards" was a
theoretical statement about a read-only tool surface. It is now a live property
of a write path, so it is worth stating exactly, and **every shorter version of
this rounds up**:

> **The sending is unguarded. The acting-on-it inherits the recipient's ordinary
> guards — which are guards on the OPERATION, not on WHO ASKED.**

"The recipient is guarded, so the buddy is safe" is the rounding-up to avoid,
and it is wrong twice over:

- **Coverage.** The recipient's `PreToolUse` hooks cover `Bash`, `Edit`,
  `Write`, `Read`, `Grep`, `Glob` and `mcp__agentwire__(email_send|quo_send)`.
  A recipient acting through any other `mcp__agentwire__*` tool —
  `session_send`, `pane_spawn`, `msg_send`, `worktree_*` — is not guarded at
  all.
- **Kind, which matters more.** Damage control guards *operations*. It cannot
  distinguish "the human asked for this" from "the buddy asked" from "a
  mis-transcription asked". So it is not a guard on the buddy's **authority** in
  any sense: the recipient is exactly as guarded as it was before, and the buddy
  has added a new way to ask it things.

What actually constrains the buddy is the frozen argv and the confirm gate
(§4a). Measured, not argued — see [the empirical result](#the-empirical-result).

#### The empirical result

Reproducible with `tools/voice_dc_probe.py`. Measured 2026-08-06 against the
**live** rules at `~/.agentwire/damage-control` (15 files, sha `af286f2c`) and
tooldefs at `~/.agentwire/tooldefs` (10 files, sha `3da4c920`) — both named
because per #916 they drift independently, and a safety claim that does not say
what it was measured against is not a claim.

| Probe | Result |
|---|---|
| **A** hook fed a `PreToolUse` payload for a `msg send` whose BODY discusses a guarded operation | **BLOCK** — `rm with recursive or force flags` |
| **B** the identical argv via `subprocess.run` from plain Python (what the bridge does) | **exit 0, queued** |
| **C** control: innocuous `msg send` through the hook | ALLOW |
| **D** control: a genuinely destructive command through the hook | BLOCK |

C and D are not decoration. Without C, A does not distinguish "the rule fired"
from "the hook refused for some other reason"; without D it does not distinguish
"the rules work" from "everything is blocked".

So both halves hold: the Bash-tool path is hooked and **over-blocks on prose**
(#915 reproduces), and the bridge's path **is not hooked at all**. Not "guarded
and occasionally over-blocked" — *not guarded*.

`anchored` appears **0 times in both the live and the bundled `core.yaml`**, so
#915 reproduces against the **shipping** rule set. The #916 drift does not
weaken this result.

Two adjacent consequences worth carrying forward:

- **`mcp__agentwire__msg_send` is not in the matcher list either.** So the "fix"
  a future reader reaches for is the MCP tool, and that path is *also*
  unguarded. The choice is not "unguarded vs guarded-but-over-blocking" — it is
  **unguarded, unguarded, or over-blocking**. `msg send` has no damage-control
  coverage on any programmatic path.
- **Routing the buddy's writes through a Claude Bash tool to "fix" this
  inherits #915**, and produces a buddy whose write is refused for *describing*
  an operation. Out of scope here; noted so the next person sees the trade.

Two traps if you re-run the probe. Launching it through Claude Code's Bash tool
means your own hook matches the prose in the command line (#915) and the probe
never runs — hence the pattern is assembled from fragments inside the file.
And running the hook under bare `python3` returns exit 2 with `pyyaml
unavailable`, which is the **fail-closed path, not a rule**, and exit 2 is the
same code a real block returns; the probe invokes the hook through its own
`uv run --script` shebang and reports a pyyaml reason as INVALID rather than as
a block.

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
         │ GET  /           the page itself — served with NO auth, so this is
         │                  the request that hands the bearer token over
         │ POST /mint       ephemeral client secret + the page's clock ORIGIN
         │ POST /tool       function_call → result
         │ POST /utterance  speech-start / commit / transcript, each carrying
         │                  the client's conversation-item sequence
         │ POST /anchor     "the proposal was SPOKEN", at this sequence
         ▼
┌───────────────────────────────────────────────────┐
│  localhost bridge (server.py) — Host-allowlisted  │
│    · mints ephemeral client secrets + a seq epoch │
│    · dispatches tool calls through the allowlist  │
│    · holds the transcript ring and confirm spine  │
└────────┬──────────────────────────────────────────┘
         │ allowlisted argv only
         ▼
┌──────────────────────────────────────────┐
│  agentwire CLI  (the documented SSOT)    │
│  list --sessions · worktree --list/--dangling · scheduler board · …
└──────────────────────────────────────────┘

   buddy identity: ~/.agentwire/sessions/buddy/metadata.json
   buddy inbox:    ~/.agentwire/inbox/buddy/     ──drain──▶  inbox-spool.jsonl
   buddy outbox:   ~/.agentwire/sessions/buddy/outbox.jsonl  (what it SENT, #958)
```

`/utterance` and `/anchor` are not plumbing — they are the confirm gate's
ordering, and §Ordering below is about what they carry. The four state-carrying
paths run through one `BuddyBridge` holding one `TranscriptRing` and one
`ConfirmSpine`, per conversation rather than per process: a module-level store
of pending writes would outlive the conversation that proposed them.

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
append-only). Nothing pushes into the conversation from this side: the drain
appends and stops. **The pull is now on a clock, though** — "this slice never
interrupts" was true of the seam and stopped being true of the layer. `client.py`
polls the spool every 5s and volunteers at a gap (#962), and escalation-kind mail
rides a relaxed gate that may cut across the buddy's own speech, never the
owner's (#967). The seam is unchanged; the sentence describing what the owner
experiences was not.

The read cursor stores the **last-acked message id**, not a line count. A count
is simpler and wrong: rotating or truncating the spool leaves it pointing into a
file that no longer has that shape, and the failure is silent — new mail reads
as already-seen and is never spoken. An id that is no longer present means the
spool rotated, and the safe answer is "treat everything as unread". Re-reading a
message is an annoyance; losing one is the bug.

**The ack names an id, not "everything" (#970).** `ack=true` advances to the
spool TAIL as it stands *at ack time* — and every consumer here reads, then
processes, then acks (the notifier deliberately acks only after SPEAKING), so
mail lands in that window and is cursor-advanced past having been read by
nobody. In a screenless channel that loss surfaces nowhere: no dead-letter, no
email, no screen. `ack_through=<id>` acks exactly through what the caller
consumed, so a later arrival is still pending **by construction**. PR #969
narrowed the window instead, catching swept messages in a page-lifetime array
whose stated residual was that a page unload dropped them silently; that array
is gone, and the property it was defending now rests on the cursor rather than
on client state surviving a page.

Refusals fail toward re-reading, never toward sweeping: an id the spool no
longer holds moves nothing (and reports so — a silently-refused ack is
indistinguishable from a successful one, and re-announces forever), ids are
matched exactly rather than trimmed into a match, and the cursor never rewinds.
**The precedence keys on PRESENCE, not on the value** — collapsing "no
`ack_through`" with "an `ack_through` that is blank" sends `{ack: true,
ack_through: ""}` down the bool path and sweeps the tail from inside the guard
against sweeping it.
The notifier acks only through an unbroken run of messages it is speaking now
or has already been heard: the interrupt tier speaks an escalation while an
ordinary report sits *ahead* of it, and one id cannot cover the alarm without
burying the report — so that tick acks nothing, and the full tick that finally
speaks the report clears both.

### Fleet awareness: two tiers, because everything in the spool gets spoken (#1016)

The buddy could be *told* things — `msg send --to buddy` reaches the spool, and
fleet detectors can address it (#982/#1002) — but nothing emitted the fleet's
own ordinary signals. A session went idle, a scheduled task finished, a toast
went up, a session spoke through fleet TTS, and the buddy knew none of it. So it
could not check in on delegated work, and fleet TTS and voice mode were two
disconnected audio surfaces in the same room.

**The routing was never the hard part.** The judgment is, and it turns on one
property of what already exists: *everything in the spool eventually gets
spoken*. The notifier volunteers unread mail at a gap; an escalation cuts
across the buddy's own voice. There is no quiet kind. A producer that pushed
lifecycle events into the spool would not make the buddy aware — it would make
it a narrator, and the owner's move against a narrator is to stop listening.

Hence two tiers:

| | Where | Who reads it | Spoken? |
|---|---|---|---|
| **Awareness** | `~/.agentwire/fleet-activity.jsonl` (`agentwire activity list`, tool `fleet_activity`) | pulled, on a question | never |
| **News** | the buddy's spool, as ordinary typed mail | pushed, by `fleet_alerts.emit` | at a gap |

The ledger is a **pull**, not a fourth push — which is what keeps the closed
list of unprompted paths in `instructions.VOICE_MODE` closed. It is also what
makes the two audio surfaces one: every `agentwire say` is recorded, and the
instructions tell the buddy that a `spoke` entry was *already heard by the
owner*, so it refers back to one rather than delivering it as news.

**What earns the spool** is `fleet_activity.ANNOUNCE`, pinned as data next to
its kind in `fleet_alerts.DETECTOR_KINDS`:

- **`session_idle` → `done`, and only for DELEGATED work.** #716's axes are
  independent — location is not authority — so the gate is **not** an OR over
  them: an interactive role (`orchestrator` on the role axis, `anchor` on the
  persona axis) is never delegated *whatever its location*, and only then does
  a recorded parent / a worker-or-reviewer role / a worktree checkout count.
  The OR got this wrong in a way worth remembering: `agentwire orchestrator` is
  sugar for `worktree --kind orchestrator`, so the owner's durable window has
  `role: orchestrator`, `created_by: ''` **and** `worktree_path` set — the
  location axis overruled the role, and the buddy announced "… is idle and done
  working" every fifteen minutes into a conversation the owner was having with
  that very session. The event's own parent is also excluded, since it already
  hears this by paste from `notify-parent`; the same news twice is how a
  channel earns being ignored.
- **`task_completed` → `done`, `request` when it ended badly** — the canonical
  check-in event: the owner did not watch it start and cannot see it end. The
  inherit rule is the dead-letter rule's shape — the fleet already judged it,
  and flattening that to news throws the judgment away. `usage_limit` and
  `auth_expired` are ledger-only: both conditions have a detector of their own
  that says it once, machine-wide, where this would say it again per task.
- **`toast_high` → `request`** — `notify-user --priority high` is the one
  notify surface that declares its own urgency, about a screen the owner may
  not be looking at. An ordinary toast is ledger-only. Its throttle subject is
  the toast's **content**, not its sender: keyed on the session, "build is red"
  swallowed "deploy rolled back" a minute later, and on the one surface whose
  caller declared the message urgent a false-reject is silence with no screen
  behind it.

**Nothing lifecycle is ever an `escalation`.** The interrupt tier stays the two
conditions nothing clears without a human. An idle session is not burning.

**Throttled per subject, from the ledger itself** — 15m for an idle, 2m for a
task, 5m for a high toast. A flapping session records every flap and buys one
utterance. The check reads the ledger rather than a second state file: the
record is written *before* the emit decision, so there is one thing to keep
consistent, and a lost ledger costs a duplicate announcement rather than a lost
one.

**Producers, and why each sits where it does.** Every one is best-effort — a
producer's job is its own job:

| Surface | Seam | Recorded |
|---|---|---|
| idle hook | `cmd_notify_parent`, `--on-idle`, **above** the no-target return | `session_idle` |
| `agentwire say` | `cmd_say`, per successfully-played chunk | `spoke` (+ the sink) |
| text toasts — CLI `notify-user`, **MCP `notify_user`**, `say --display`, and the scheduler's zombie reaper (`priority=high`) | `core.post_desktop_notification`, below all four | `toast` / `toast_high` |
| `agentwire notify-event` | `cmd_notify`, three events only | `session_created` / `session_closed` / `pane_died` |
| scheduler | `report._log_event("task_completed")`, the one seam both dispatchers use | `task_completed` |

Three of those placements are the whole point.

**The idle producer sits above the "no parent, nothing to do" return** — that
early exit is exactly the case (a root session finishing) that told nobody.

**The toast producer sits below every transport.** There were three toast
producers and the MCP one POSTed on its own, so — since agents use MCP and
humans use the CLI — a CLI-side hook would have recorded exactly the toasts a
*human* posted and none of the ones the fleet posts.
`core.post_desktop_notification` is now the one call for a toast that carries
**text**, and it records whether or not the portal took it: a toast the portal
refused is the case where the voice channel is all that is left. Enrolling the
seam rather than the producers also picked up a fourth one the first draft did
not know about — the scheduler's zombie reaper posts a `priority=high` notice
when it finds a session crashed at a bare shell, so that now reaches the buddy
as a spoken `request`, which is right and is what "a new caller inherits it"
means. What deliberately stays outside is `mcp_desktop._announce_artifact`: the
#817 click-to-open notice carries no `text`, and a ledger entry with nothing in
it is worse than no entry. Same reasoning for **the scheduler**,
which hooks the shared event log rather than the two dispatch call sites — a
per-call-site copy is what drifts (the ruling the worktree-path and
session-name SSOTs keep re-teaching).

**`say` records what actually played, not what it was asked to say.** The local
path chunks, so a failure on chunk 3 of 4 records chunks 1–2 — the owner heard
those. Recording the whole string would claim they heard a sentence that never
played; recording nothing would let the buddy offer it back later as news.

**The fleet is not a colleague, and the notifier now says so.** `composeNotice`
speaks its output verbatim — the model never rephrases it — so mail from
`fleet-alerts` / `fleet-activity` rendered as "fleet-activity got back to you:
…", telling the owner that a session with a robot's name had answered something
they never sent. Machine senders now render as **"From the fleet: …"**, with no
verb (the body is already a whole statement), and an escalation keeps its
"Heads up —" alarm prefix. The JS list is kept in step with
`fleet_alerts.MACHINE_SENDERS`, which is the same list on the producing side.

**One utterance carries at most three bodies.** `composeNotice` coalesced every
unread message, which was fine while mail arrived one reply at a time and is a
monologue the moment a fan-out lands in one 5s poll window — ten workers, one
~2500-character utterance, spoken over nothing the owner asked for. Speech
cannot be skimmed and the owner cannot predict when it stops. The overflow is
**held, not dropped**: only the spoken messages are claimed, marked in-flight
and eligible to ack, so the rest stays unread and the next quiet tick says the
next three — the same "announced late, never lost" property the interrupt tier
already relies on. The notice says "And N more waiting", because a capped batch
must not sound like a complete one. This was pre-existing in the notifier;
#1016 is the first producer that reaches that scale with no human in the loop.

`agentwire notify-event`'s other vocabulary — `client_attached`,
`pane_focused`, `window_activity` — is deliberately absent: it fires on every
glance at a terminal, and recording it would bury the events that mean
something under events that mean somebody moved a mouse.

### The tool surface is an allowlist, not a passthrough

The model chooses *which* tool. It never chooses *what runs*. Every tool builds
its own argv from validated parameters.

The live surface is **27 read tools plus one gated write spec**, and the spec
generates three tools (`propose_` / `send_` / `cancel_session_message`), so the
model sees 30 names. **Do not maintain a list of them here** — an enumeration in
prose is what went stale last time, and `agentwire buddy tools` prints the exact
array handed to the model. What belongs in a wiki is the rule that decides what
may ever appear.

#### The tier audit is the ruling document

`agentwire/voice_layer/surface.py` (#966, extended by #979) places **every** tool
name in `agentwire/mcp_*.py` in exactly one tier, and a test parses those modules
and fails the moment a new tool ships untiered. Classify by what the action
touches; first clause that applies wins:

| Tier | Rule | Wiring |
|---|---|---|
| **read** (`TIER_READ`) | observes only | direct dispatch. Expand freely — a read the buddy lacks is a question it has to deflect |
| **write, light** (`TIER_WRITE_LIGHT`) | the wrong execution is undone by ONE action of the same kind, destroys nothing, and causes no agent or human to act | confirm-FREE by design |
| **write, gated** (`TIER_WRITE_GATED`) | causes another agent or human to act, changes durable state, or destroys something | only ever through the confirm spine (§4a) |
| **excluded** (`TIER_EXCLUDED`) | see the lettered clauses below | never reachable, by design and not by omission |

Excluded is (a) creates or drives an agent session, (b) is another output channel
to the owner, (c) publishes outward, (d) authors work product, (e) mutates
infrastructure identity. Two of those clauses are subtler than they read, and
both were re-argued once already:

- **(a) keys on the DISPATCH PATH, not the verb.** Anything reaching `agentwire
  ensure` — `task_run`, `scheduler_run` — creates the session when it is missing
  and then drives it to completion, so it is (a) whatever it is called. The
  carve-out "but the task content is owner-authored, in the protected
  `.agentwire.tasks.yml`, behind a nonce" was considered and **rejected**:
  authorship of the prompt does not change who instantiated and drove the
  session. A test walks every tier-1/2 tool's argv into the CLI call graph, so
  the next ensure-shaped verb cannot land under an innocuous name.
- **A light grade is a positive ruling, not laxity.** A nonce on "open a window"
  is not merely unnecessary, it is corrosive: a confirm phrase for something
  trivial trains the owner to speak the nonce reflexively, and a reflexive nonce
  is a dead gate. Price both halves of a guard.

Two #979 rulings recorded because a tier move with no reason gets re-argued:
**`scheduler_report` is excluded, not a read** — the name says report but the
call writes an HTML artifact and can push a portal notification, which is (d)
plus (b); and **`pane_detach` is excluded, not gated** — its target session is
"created if doesn't exist", so a mis-heard name INSTANTIATES a session rather
than misfiring a move, and the dispatch-path analyzer cannot see it.

**Tiering is capability classification; WIRING is a smaller set.** Everything
live must map into tier 1 or 2, and a test asserts the excluded names are absent
from the realtime surface **by name**. Today exactly one gated write is wired
(`msg_send`) and exactly one light write is (`buddy_inbox(ack=true)`, which
advances the buddy's own read cursor); the other light candidates are unwired
only because they have no CLI verb, and the voice layer dispatches only through
the CLI.

**The map is checked against reality, not against itself.** A hand-written map
that nothing verifies is the same over-claim this module polices one level up —
and it had one: `fleet_session_output` pointed at `sessions_list` with the whole
suite green. The audit now runs each read tool with the CLI stubbed and compares
the argv it really builds against the argv the mapped MCP capability builds.
Where that check has no purchase it is **stated at its real size**: fifteen
capabilities build no extractable argv, so a mapping onto any of them is
unfalsifiable rather than verified, and the exemption is granted per
(tool, capability) PAIR — name-scoped, one recorded exemption silently covered
all fifteen. Exactly one wired mapping needs it (`fleet_wiki_search` →
`wiki_query`) and that one rests on a human having read it. A weaker residual,
unfixed and named: the comparison is a prefix match, so a capability whose argv
is a single token corroborates any voice argv starting with it.

**Tools with no MCP capability behind them are ruled too** (#979). `buddy_inbox`,
`buddy_sent` and `fleet_pull_requests` have none, so for a while "every tool
appears in exactly one tier" was true of a namespace that is not the exposed
surface. `surface.VOICE_NATIVE` carries a written grade and reason for each, and
`surface.unruled_tools()` — what the audit calls — makes a new ungraded
voice-native tool red.

#### Names still fail closed, and `@` is not the gate

A garbled session name **fails closed** and comes back as a spoken question, not
a fuzzy match. Two real injections were caught by tests while building this,
both from `-` and `.` being legal name characters:

- `--help` matched a naive pattern and reached the CLI **as a flag**.
- `../etc/passwd` matched and became a path.

Fix: every segment must start alphanumeric. Both are covered by tests. The same
leading-dash hazard governs free text (`_query_arg` strips controls, bounds
length, and removes leading dashes) and the rendered body, whose leading-dash
guarantee is an explicit assertion rather than a happy accident of layout.

**Remote `name@machine` targets are out of scope (owner ruling, 2026-08-09) —
and the gate is LIVENESS, not the `@` character.** The first attempt at the
ruling refused any name containing `@`, and that was itself a false statement:
`@` does not mean remote. tmux accepts it verbatim (only `.` and `:` are
rewritten, #878) and `inbox._SESSION_RE` admits it, so `ops@edge` is a creatable,
addressable LOCAL session the buddy was telling the owner was unreachable — a
confident falsehood with no move from it, which is the expensive failure in a
channel with no screen. So `tools._session_arg` validates the SHAPE first (a
garbled name that happens to contain an `@` is a mis-transcription and gets the
mis-transcription answer), then consults `inbox.live_sessions()`: a whole name
local tmux reports live is local **by demonstration**. What that refuses is
exactly a name nothing local answers to — every genuinely remote target — spoken
as the one thing measured ("there's no live session called X on this machine"),
never as a diagnosis of where it lives. An unreachable tmux proves nothing and
so refuses nothing.

Errors come back as **data, never exceptions** — a stalled function call leaves
the conversation hanging, whereas an error can be spoken ("I don't have a
session by that name — which one did you mean?"). Every refusal carries `say`
plus `must_speak`, so there is no path by which one reaches the model as
something it can quietly swallow and retry around.

---

## 4. What this slice does and does not do

**Does:**
- Buddy identity + inbox + the delivery adapter.
- Read-only fleet awareness: what is running, what is blocked, what needs you.
- Reads its own mail from other sessions.
- Reads back **what it has sent**, verbatim, with a live delivery state
  (`buddy_sent` over the outbox, #958) — so "did that word end up in the
  message?" has an instrument instead of a recollection.
- **One write: a message to a session that is already running** (§4a below).
- Volunteers mail at a gap (#962) — whatever a session sends it, not only
  replies to things it sent; re-raises an unactioned request once (#967);
  escalation-kind mail may cut across the buddy's own speech, never the
  owner's (#967, Q3 below).

**Does not — and this is where the risk lives:**
- ❌ No spawning, no session creation, no worktrees. Ever. See [Cold fleet](#cold-fleet-the-buddy-never-starts-an-orchestrator).
- ❌ No acting directly on the fleet — every write is a request to a session.
- ❌ Never speaks while the owner is speaking — unconditional for every tier,
  including escalations. And never inside a confirm handshake, whose window is
  **wider than the anchor** (#978 item 2): `canInterrupt()` requires both
  `!confirmGate.outstanding()` **and** `!announcer.anchorPending()`, so it is
  already closed while the proposal is merely queued or mid-announcement, and
  stays closed until the outcome or the TTL. Before that fix an escalation
  ticking in exactly that window queued behind the proposal and `pump()`
  promoted it the instant anchoring closed the gate — an alarm spoken between
  "say confirm tango" and the owner's answer.

There is deliberately **no escape hatch**. Adding a capability means adding a
tool, in a diff someone reviews — and since #966 that is the *weaker* of the two
statements. The stronger one is the tier audit in §3: a capability now has to be
placed by a written rule before it can be wired at all, and a test fails the
moment an untiered tool ships.

## 4a. The confirm spine

The buddy's one write is gated below the model, in two halves that do different
jobs.

### The guarantee, in full

> This defends against **mis-transcription and against an approval the
> conversational model invented**, which is the stated threat. It does **not**
> cover every mis-transcription — a transcriber hallucination or an
> approval-shaped utterance meant for someone else is a real residual risk that
> the nonce narrows but does not eliminate. **A spoken retraction is caught only
> when it uses a word or phrase the grammar knows** — "let's not", "on second
> thought" and "I changed my mind" are not caught, and no word list reaches
> them. **A passed gate means the message was queued, not delivered, and not
> acted on.** The `said:` clause is evidence of what was **heard**, not proof of
> what was **said** — it is exactly as trustworthy as the local browser page,
> which holds the bridge token and can POST to `/utterance`. It is **not** a
> security boundary against an adversary.

The `said:` clause is the fourth caveat and it is the newest. §4b's whole purpose
is that the verbatim request utterance is evidence a recipient can CHECK the
paraphrase against, and a recipient reading `said:` will treat it as what the
human said. Anything resident in the bridge's page holds the per-run bearer token
and can POST arbitrary text to `/utterance`. The residual is small for a reason
kept deliberately OUT of the quotable sentence — stacking mitigations into an
honest limit is how it gets rounded back up — but it is worth knowing once: that
field reaches only the attribution clause. `--to`, `--from`, `--kind` and the
instruction are all frozen at propose, so the worst available consequence is
falsified *evidence*, never a redirected write.

The retraction clause is a **stated residual, not a to-do.** Chasing "let's not"
/ "on second thought" is how this becomes the unbounded denylist the filler list
already taught us to reject. What bounds the damage is that a missed retraction
approves nothing by itself — the write still needs the nonce, so the owner can
simply not say it. The residual is "said something meaning stop, AND then said
the nonce anyway", which is narrower and stranger than the clause's plain
reading suggests.

**Widen this if you learn more; never narrow it.** Do not paraphrase it as "the
confirm gate protects writes". The "queued, not delivered" clause is here rather
than only next to the spoken wording because this paragraph is what a future
reader quotes when they ask what the gate guarantees — and without it they
conclude "gate passed, so the write happened". It did not.

### (a) Proposal binding

The write tool refuses any call lacking a token minted on a prior turn, and the
**argv is frozen at propose time**. `confirm` takes exactly one argument — the
token — so there is structurally nothing to mutate between propose and confirm.
TTL-bounded, and **single-use means consumed on SUCCESS, not on attempt**: if a
refused attempt burned the token, the "give me a second" refusal below would be
telling the owner to wait when waiting cannot work. Refused attempts are
rate-limited instead (`MAX_CONFIRM_ATTEMPTS = 5`, and the attempt that hits the
cap reports `too_many_attempts` rather than `refused` — telling the owner to say
the phrase again at the exact moment that stopped working is the taxonomy
collapse §3.4 forbids).

**Single use is a property of the CLAIM, not of the timing** (#987). `_claim()`
takes exclusive ownership of the token before the await and the judge; a second
confirm carrying the same token gets `in_flight`, a wait outcome that burns no
attempt and does not close the gate out from under the confirm that is actually
running. The old design popped the proposal only at the far side of the runner,
so two confirms could both pass and both dispatch. It was not reproducible —
client dispatch is sequential per response and the judge window is
sub-timeslice — and that is exactly the argument this module does not accept:
each `response.done` spawns its own async IIFE and the bridge is a
`ThreadingHTTPServer`, so the sequencing that made it safe was nowhere in the
code.

**`cancel()` takes the SAME claim** (#990). It used to bypass `_claim()`
entirely, so a cancel racing a dispatching confirm popped the proposal and said
*"I heard you hold off, so I haven't sent it"* while the runner was sending —
the same race `in_flight`'s wording exists to avoid making a false claim about,
left uncovered on the sibling path. Screenless, that is not a lost cancel: it is
an affirmative "nothing was sent" the owner has no way to check.

Three things make the shared claim correct rather than merely shared:

- **"A confirm holds the claim" is not "the write is going out",** and the
  first fix for this re-committed the original defect by conflating them. The
  claim is taken at the TOP of `confirm()`, *before* the ≤2.5s await and the
  judge, so its dominant occupant is a confirm still DECIDING — and telling a
  cancel there "it's already going out" is false, while leaving the proposal
  the owner asked to drop still pending. The race is therefore split on the
  thing the sentence is about: `_dispatching` (inside the runner — the only
  state in which the claim is true) versus `_cancelled` (a cancel during the
  await WINS; it answers `denied`, and the confirm re-reads the marker under
  the same lock immediately before dispatch, so the write does not go out
  behind the retraction).
- The losing cancel gets its **own** outcome, `cancel_in_flight`, not
  `in_flight`. They answer different questions — "should I confirm again?"
  (no, wait) versus "did you stop it?" (no, and here is why) — and its spoken
  line names the move that is still open, because the one thing the owner asked
  for is the one thing no longer available.
- **The barrier is ONE lock hold**, and that is the claim the redesign rests
  on: reading `_cancelled` and then marking `_dispatching` in two holds leaves a
  gap in which a cancel is told, truthfully by then but wrongly at the moment it
  spoke, that the write was going out — #990 in its original form, restored by a
  refactor. Pinned by sweeping *every* observable lock boundary rather than the
  one that happens to be the barrier today, so moving it does not silence the
  test.
- **The recorded outcome outranks EVERY in-progress marker**, and it took two
  goes to get that right. A confirm records its result and only *then* unwinds,
  so a token sits in `_succeeded`/`_failed` while still marked. Testing
  `_in_flight` first answered a cancel there with "I haven't sent anything"
  about a write that had gone out — the third window. The rule that fixed it —
  `_succeeded`/`_failed` are facts about the WRITE, `_in_flight` and
  `_dispatching` are facts about the ATTEMPT, and only the first kind can
  contradict a spoken claim about sending — was then applied against
  `_in_flight` **and not against `_dispatching`**, leaving the identical hole on
  the other marker: between `_failed.add` and `_dispatching.discard`, a cancel
  was told *"Too late to stop that one — it's already going out … we can undo it
  from there"* about a dispatch already recorded as FAILED. Two definite claims,
  about the one outcome `dispatch_failed` exists to say cannot be characterised.
  Both terminal checks now sit above both markers, and the property is pinned as
  the cross-product rather than at either site. **A rule enforced against one of
  two markers is not a rule.**
- **A cancel is never answered with confirm-shaped advice.** The claim is
  shared so the two paths cannot drift, but two of its lines argue for the very
  write just retracted — `no_proposal` says *"Tell me again what you'd like
  sent"* and `expired` says *"Ask me again and I'll set it up fresh"*. On the
  cancel path both become `nothing_to_cancel`. Collapsing them is consistent
  with the taxonomy rule rather than an exception to it: outcomes stay apart
  when the owner's next move differs, and here both mean "there is nothing of
  mine to take back", whose next move is none. `replayed` and `dispatch_failed`
  pass through unchanged — both are true of a cancel and neither invites a
  re-propose.
- **No cancel outcome leaves a live proposal behind**, pinned as a property
  over every reachable one rather than at the site that used to break it.
- **A successful cancel says `cancelled`, not `denied`.** `denied`'s advice —
  *"Say the phrase again when you're ready"* — is true only of an in-band
  denial, which leaves the proposal LIVE. A cancel pops it, so following that
  advice lands on `no_proposal` (*"Tell me again what you'd like sent"*) one
  turn later: the very line the translation above exists to keep off this path,
  re-entering through another door. `cancelled` is a pure stand-down — the owner
  asked for this, so nothing further is required of them, and it deliberately
  does not offer to set the write up again.
- It is a **wait** outcome, so `confirm_terminal` stays False: closing the
  handshake here would close it out from under the confirm still running.
- The announcement requirement is **dropped** (`require_announced=False`). "The
  buddy hasn't finished saying it" is a reason to refuse a confirm — the owner
  cannot have approved what they have not heard — and not a reason to refuse a
  RETRACTION, which needs no announcement to be meant. Refusing one would leave
  the proposal live for its full TTL over who was talking. Everything else in
  the claim applies to both callers, so a cancel after the write really went out
  now says `replayed` instead of asserting it did not.

### (b) The approval judgment: a spoken nonce

DocumentScribe leaves this entirely in the model (§5). We do not.

The buddy speaks a nonce in the proposal — *"say **confirm tango** to approve"* —
and the approval grammar is `confirm <nonce>`, evaluated **in code** against the
transcription model's output. The conversational model's claim to have heard a
yes never enters into it.

An earlier design used an approval grammar plus a filler denylist, and claimed
that meant "two models must fail the same way". **That was false and the
mechanism was weaker than it looked**, because the two models consume the same
audio. Three breaks, none needing the conversational model to fail at all:

- **Transcriber hallucination.** `gpt-4o-mini-transcribe` is Whisper-lineage,
  and confident short affirmatives on near-silence — "Okay.", "Yeah.", "Thank
  you.", "Yes." — are that family's best-documented failure. Three of those four
  were *in the denylist*, which is the tell: **the denylist was enumerating a
  hallucination prior.** An unbounded denylist is not a mechanism; it is a list
  of the failures you have thought of so far.
- **An approval-shaped utterance meant for someone else** — "yeah, that's
  right, anyway" to a person in the room. `semantic_vad` commits it.
- **One approval, two proposals.** The condition was existential ("there is an
  utterance after the proposal"), so one "yes" satisfied both. That is the
  "acting twice" failure §4b names.

The nonce **narrows** the first two and **closes** the third (nonces are unique
among live proposals; running out is an error, never a reuse). It is not
"kills" — a transcriber can still hallucinate and a word can still occur in
speech meant for someone else, which is why the honest limit above says what it
says.

**The nonce alphabet is words, not digits, and that is a livelock fix.** "four
seven" comes back as `47`, `four seven`, `4-7` or `forty-seven` — the least
stable token type paired with the strictest matcher, so a **correct** approval
fails deterministically, and the taxonomy reports it as "say it again", so the
owner repeats and fails identically. Pricing the false-accept half without the
false-reject half produces a gate nobody can pass. Hence normalization on both
sides, and **containment rather than whole-utterance matching** — strictness was
inherited from a grammar ("yes") that carried no entropy, and the nonce carries
its own.

**The selection rule is "one TRANSCRIBER RENDERING each", which is stronger than
"one spelling"**, and that difference cost two of the original twenty words.
`harbor` — a Whisper-lineage model emits en-GB `harbour` freely. `ripcord` — a
compound, and `rip cord` is an ordinary segmentation. Neither variant is in the
alphabet, so the outcome is not even `wrong_nonce`: it is `no_match`, whose
advice is "say confirm and then the word I gave you", so the owner repeats the
identical utterance, fails identically, and the proposal retires at the attempt
cap. That is the digit failure exactly, reached through spelling instead of
digits. They were **removed rather than aliased**: a variant-folding map can only
fold token-for-token (`rip cord` is two tokens), and folding a spelling for a
word nothing mints is machinery with nothing to do. The rule replaces both —
**one morpheme, no en-US/en-GB split.**

Disfluencies are skipped **between the confirm word and the nonce**, for the same
reason the denial grammar strips them before matching: "confirm, uh, tango" is
the phrase, said by someone hesitating before a code word, which is exactly how
people say code words. Requiring strict adjacency refused a correct approval and
burned an attempt — the false-reject half, which in this channel is a silent
loop. Safe by the file's own asymmetry: both content words are still required, in
order, and an unlisted filler fails CLOSED.

**`quoted_frame` is the announcement-frame echo defence.** The buddy's own
proposal line is "…To approve, say confirm tango", and `speechSynthesis` audio is
outside WebRTC echo cancellation, so a fragment of it can land in the USER
transcript. The structural fix is that the fallback channel never carries the
nonce; this is defence in depth for the frame itself — "confirm" immediately
preceded by "say", in an utterance that also frames with "approve", is quoted
instruction, and no human phrases an approval that way. Deliberately narrow (both
conditions), because refusing a bare "say confirm tango" from an owner parroting
the advice line would loop them against advice that says exactly those words. It
is its own outcome rather than folded into `wrong_nonce`, because "that was a
different code word" is FALSE here: the word was right, the framing refused it,
and sending the owner to re-ask for a code they already have fixes the one thing
that was not broken. What it does **not** establish: an echo chunked down to a
bare `confirm <nonce>` with the frame lost still approves; only the nonce-free
fallback text closes that.

### Ordering: conversation-item time, on speech-START

"The approval postdates the proposal" is only well-defined if both sides are the
same quantity. Two wrong answers, both of which silently invert the predicate:

- **Transcript arrival time.** That is when transcription *finished*, and the
  gap from when the audio was *spoken* is exactly the latency the hazard is
  about. An utterance spoken before the proposal but transcribed after it stamps
  as postdating it.
- **The audio COMMIT event.** Commit fires at the *end* of an utterance, and the
  barge-in case is the owner starting to speak *during* the proposal and
  finishing after it — speech-start predates the proposal's `response.done`, the
  commit postdates it. **Ordering on the commit approves the barge-in**: the
  exact hole the clock change exists to close.

So both sides move to a logical clock — a sequence the client assigns in
data-channel event order:

- an utterance is stamped at **`input_audio_buffer.speech_started`** (the intent
  time; the commit is recorded for binding and inspection, and never gates);
- a proposal is anchored **on positive evidence that its announcement was
  SPOKEN** — see below.

The transcript forward is awaited before any function call dispatches — they are
independent `fetch` calls otherwise — and the ring holds a lock, because the
bridge is a `ThreadingHTTPServer` and a confirm blocks on the ring's condition
waiting for the transcript it needs.

#### The anchor is EVIDENCE, not the next `response.done` (#951)

The wording this section retired — "anchored at the `response.done` of the turn
in which the buddy spoke it" — described the intent, and was also the
implementation until it broke. Read
literally as *the next `response.done` carrying any text*, the announcer's own
cancel could steal the anchor, and a proposal spoken by the **fallback voice** —
which produces no model turn at all — was anchored by nothing: the owner hears
the proposal, says the nonce, and gets `not_announced` until the TTL. That is the
one corner where the two safety mechanisms defeat each other. `not_announced` is
never *silent* — it speaks correctly every time — but it can be persistently
WRONG, and what made it wrong was the fallback firing, which is the mechanism
added to GUARANTEE speech.

So the anchor is driven from the announcer's `onSpoken(meta, how)`, which fires
in exactly two cases and both are positive evidence:

- **`"model"`** — a `response.done` whose transcript actually carried the
  announcement, judged by unique-content-word overlap rather than equality (the
  model is *told* to say it exactly, and prompt compliance is not a mechanism);
- **`"fallback"`** — the browser voice said it. In a robot voice, but the owner
  heard it, so anything keyed on "was this spoken" must be told so, or the
  fallback that guarantees speech becomes the reason a correct nonce is refused
  forever.

Only then does the page `POST /anchor` with a fresh sequence. A **cancelled**
response is never evidence — it can carry partial audio that said something else,
and the announcer produces one on every refusal — and a torn-down announcer
reports nothing as spoken, because an item killed by `stop()` was not heard.

#### The clock's ORIGIN comes from the bridge (#978)

The page assigns the order; it cannot own the origin. `seqCounter` is a page
variable that restarts at 0 on every reload, while the ring and the spine live
for the whole bridge run — so a reloaded page anchored its proposals BELOW last
session's utterances, which are still in the ring, complete and unspent. Those
reached the judge as non-matching (burning attempts on a question never asked)
and, worse, an old "no, hang on" sat strictly-after the new match in the
post-approval denial scan and **retroactively denied every legitimate approval**
until 32 fresh utterances evicted it.

`/mint` is the one event that happens exactly once per page load, so it hands out
the origin: a whole `MINT_SEQ_GAP` (1,000,000) above every sequence the bridge has
seen, reserved **under one lock** (`TranscriptRing.reserve_epoch`) because
read-then-write is two acquisitions and two concurrent mints on a threading
server can be handed the same base — the very case the epoch exists to rule out,
reintroduced inside the fix for it. Reserved BEFORE the client secret, so an
exhausted sequence space refuses without spending the owner's API key. Nothing is
rejected and nothing deleted: a rejecting epoch guard would pay its false-reject
half by dropping an utterance from the owner's LIVE tab, and a dropped utterance
here is a silent loop.

Sequences are ceilinged at `MAX_SEQ = 2**45`, and the reason is that the number
now crosses a JSON boundary back into the page, where it is an IEEE-754 double:
past `2**53` an increment silently stops advancing, so every event shares one
sequence, `after(anchor)` is never strictly-after, and the buddy answers
`pending_transcript` forever; larger still parses as `Infinity`, whose anchors
serialize as `null` — `not_announced` forever. Both are silent and both survive a
reload, because `high_seq` is bridge-lifetime. Out-of-range is **refused, not
clamped**: clamping would still raise `high_seq` toward the ceiling, and a
silently-altered sequence is a silently-altered ordering. `2**45` leaves ~35
million mints of headroom, so the false-reject half costs nothing real.

### Bounded await, and outcomes that differ

Fail-closed *immediately* is wrong: the conversational model starts generating as
soon as VAD commits while transcription is a separate pass, so a confirm often
beats its own transcript. Refusing instantly would tax every confirm two
utterances — and leave the first approval stale in the ring, so a retry after
"no, wait" would **write after the owner said no**. So the gate waits ~2.5s on a
condition variable, a matched utterance is *spent*, and any denial committed
after an approval refuses.

The post-approval scan is **bounded, and the bound moved once**. Unbounded, an
utterance from a different context — including one arriving during a *retry's*
await — retroactively denies and reports "you said no" about something said
somewhere else. But bounding it to the snapshot taken when `confirm` was *entered*
left a real hole: a denial the owner BEGINS during the ≤2.5s await records its
speech-start above that snapshot and carries no transcript yet, so the write went
out with a take-back mid-transcription. The ceiling is now read twice — before
the await and again after — so the window is "everything the owner had started by
the time this confirm reached its verdict", still bounded and still strictly
before the verdict. The denial half of the same asymmetry is covered too:
`unheard_between` sees an utterance whose speech-start was recorded but whose
transcript has not landed, and "cannot yet say what they said" is
`pending_transcript`, never approval.

**The price used to be open-ended, and closing it (#989) took TWO bounds rather
than one.** `unheard_between` had no staleness bound at all, so any
`speech_started` with no transcript to follow — a cough, a VAD blip, TTS bleed —
sat in that window refusing every confirm as a WAIT outcome: no attempt burned,
`too_many_attempts` never fired, and the owner heard "give me a second" for up
to 120s and then "that one expired". A spoken loop, which in a screenless
channel is the expensive failure.

The obvious repair — one flat wall-clock age — is wrong, because two failures
hide under "the transcript never came" and they price oppositely:

- **Transcribed but empty.** A cough the model renders as `""` is
  `complete == False`, which is what put it in the window; it needs *no clock at
  all*. `Utterance.transcribed` records that a transcript EVENT arrived, and
  `unheard_between` filters on that instead. An empty transcript positively
  carries no denial, so the false-reject cost is zero. `complete` still gates
  `after()`, where the question is "can this text approve" and the answer is no.
- **Never transcribed.** Here a real bound is needed, and it keys on whether the
  audio buffer **CLOSED**. With a `commit_seq` the utterance is over and the
  transcript is merely overdue — `UNHEARD_COMMITTED_GRACE_S` (10s, comfortably
  past the ~2.5s an ordinary short-utterance transcript costs). Without one the
  owner may still be mid-utterance, so the bound has to exceed a plausible
  utterance — `UNHEARD_OPEN_UTTERANCE_S` (60s), well under the 120s TTL or it
  would close nothing. Ageing out on the committed grace instead would stop
  waiting for a retraction that is halfway spoken: the acting-twice direction,
  not a wait.

The division of labour is unchanged: `_judge` still cannot tell a
never-completing entry from a slow one without the ring's clock, so the bound
lives in `transcript.py` and the caller says so.

**Scope the residual precisely if you are reading an older account of it,
because the epoch section two above misleads.** The blocking entry has to be one
whose sequence lands inside `(match.speech_started_seq, ceiling]`, so what it
took was a proposal anchored **in the SAME PAGE LOAD as the entry** — not "only
within one page load". `/mint` hands each load a base a whole `MINT_SEQ_GAP`
above everything before it, which primes a reader to conclude a reload cleared
it. It did not: the ring is bridge-lifetime and outlives the page.

Outcomes are keyed on **what the owner should do next**, and are never
collapsed. This is `confirm.REASONS` in full — the SSOT for the taxonomy, and
the set `SPOKEN` is checked against **both ways**:

| Outcome | Owner's correct next move |
|---|---|
| `no_proposal` | restate the request |
| `expired` | ask again |
| `not_announced` | **wait** — the buddy hasn't finished saying it |
| `replayed` | nothing — it already went out |
| `refused` | say the phrase |
| `wrong_nonce` | ask what the word was |
| `quoted_frame` | say confirm and the word on its own — the word was right |
| `denied` | say the phrase again when you're ready — the proposal is still live |
| `cancelled` | nothing — you retracted it and it is gone |
| `pending_transcript` | **wait** |
| `in_flight` | **wait** — that confirm is already running |
| `cancel_in_flight` | **wait** — the cancel lost the race *to the runner*; it is already going out |
| `nothing_to_cancel` | nothing — there was nothing of ours left to take back |
| `too_many_attempts` | ask again from the top; that proposal is gone |
| `dispatch_failed` | check that session, *then* decide — it may have gone out |

`refused` and `pending_transcript` demand *opposite* behaviour. Collapsing them
trains the owner to repeat into a system that needed them to hold still. Four
outcomes carry that "hold still" property, and they are named once rather than
inferred from the table: `WAIT_OUTCOMES = {pending_transcript, not_announced,
in_flight, cancel_in_flight}` drives two flags on the payload —
`owner_should_wait`, which the persona consumes as a FLAG and never by outcome
name, and `confirm_terminal`, which is the name-independent signal that this
outcome ENDS the handshake. `in_flight` belongs there on both counts: the owner
should wait, and closing the gate on a duplicate would close it out from under
the confirm that is running. `cancel_in_flight` is the same two counts reached
from the cancel side (#990).

**`denied` does not say "you said no".** It covers "wait"/"hold on" as well, and
those are not a refusal of the write — the spoken line is *"I heard you hold off,
so I haven't sent it. Say the phrase again when you're ready."* A reason that
misinforms is the defect the taxonomy exists to prevent, and for one round after
the behaviour underneath it changed this row still said
"nothing — you said no".

Two guard properties worth keeping straight, because only one of them is
obvious. Checking "every outcome has a line" catches a mute refusal; it lets **a
line without an outcome** ship as dead code, which is exactly how
`too_many_attempts` shipped a carefully written sentence with no producer while
the attempt that really retired a proposal told the owner to repeat a phrase that
had just stopped working.

### Every refusal speaks — and returning a reason does not achieve that

Silence is the one unacceptable failure mode. The gate *will* refuse correct
approvals; the owner is not looking at a screen, so a refusal they cannot hear
is indistinguishable from not having been heard, and they simply repeat
themselves.

Returning a reason string does not make the model say it: a
`function_call_output` is context. Refusing to leave the *judgment* in the model
and then leaving the *announcement* in it is the same defect one level up. And
there is a genuinely silent branch: `maybeCreateResponse` declines while a
response is in flight, so the output lands and **no response is created** — the
*likely* path for a timing refusal, because a timing refusal fires exactly when
VAD is producing its own responses.

So refusals go through an announcer in `client.py`: cancel the in-flight
response (only when the client's mirror says one is active — the error a
no-target cancel generates is suppressed rather than announced, or the error
handler feeds itself; #950), issue a scripted `response.create`, and verify
against the following `response.done`.

Two #950 lessons live on this path. **`say` is literal text to utter, never a
directive to the model** — the one payload that carried a directive got read
aloud verbatim, the transcript-match disarm could then never fire, and the
timer double-spoke every proposal. And **the `speechSynthesis` channel is
outside WebRTC echo cancellation**, so its audio can re-enter the microphone
and land in the *user* transcript: whatever it utters is a string the confirm
gate may be fed. A proposal therefore ships a separate `fallback_say` with no
nonce in it — an echo of the fallback cannot carry an approval, structurally.
`WriteSpec.__post_init__` raises at import on a `fallback_template` containing
`{phrase}` or `{nonce}`, so the property is enforced rather than remembered.

**That closes the approval direction structurally. The denial direction needed
its own answer, and it is now CLOSED (#992)** — see [The buddy's own voice
cannot deny](#the-buddys-own-voice-cannot-deny-992) for the rule and its price.
The failure: `carries_denial` is not nonce-gated, and several `SPOKEN` lines
carry denial triggers, so one echoed into the approval→confirm window
retroactively denied the owner's legitimate approval — invisible to them, who
heard "I heard you hold off" about a take-back they never spoke.

**Both mitigations originally proposed were REJECTED, and the reason is the
transferable part.** Marking ring entries transcribed during fallback speech,
and suppressing the scan window, are both TIMING rules — and barge-in over the
robot voice is the normal way to retract in this channel, so "utterances during
fallback speech do not count as denials" drops a genuine take-back and the write
goes out. That is the acting-twice direction, strictly worse than the wrongful
refusal being fixed. The shipped rule is CONTENT (`confirm.is_buddy_echo`): it
cannot fire on words the buddy never said, so it has no such half. Do not
reintroduce a timing rule here without pricing that.

**The `speechSynthesis` fallback is armed by a timer, not triggered by a
detected failure**, and that is the part that decides whether this property is
real. `responseActive` is a client-side mirror and stale by construction: if the
client skips the cancel while the server has just started a VAD response, the
server rejects the overlapping create and the announcement is dropped
*server-side*, with every client-visible signal reporting success. `send()` is
fire-and-forget; nothing correlates a later `error` with a specific create.
Every failure mode here is unobservable from the client, so any design routing
the fallback through *detecting* failure leaks exactly the cases that matter.

> **Start the timer at refusal time. Disarm it only on positive confirmation
> that the reason was spoken. Default-on, disarmed by success.**

That is the general shape for anything that must not be silent: make speech the
default and require evidence to suppress it. **A refusal that always speaks in a
robot voice beats one that usually speaks in a nice one.**

**Trade, so it is not a surprise:** cancelling cuts the buddy off mid-sentence,
sometimes mid-proposal. That is the right trade here.

#### The timer DEFERS, and a deferral is not a suppression

"Default-on, disarmed by success" is the shape; the implementation adds two
bounded deferrals, and neither can cancel the timer — each is counted per item
and the re-armed timer eventually speaks with no condition left to fail.

- **Never over the owner, re-checked at fire time.** The gate that promised this
  ran when the announcement was *queued*; the fallback speaks 6s later, and until
  #978 the injected deps did not expose the signal at all, so the promise held
  for exactly the moment nobody was speaking. Bounded at
  `maxOwnerDeferrals = 3`, and the bound is the point: a fallback that waits for
  silence forever is a refusal the owner never hears.
- **One in-flight deferral**, on one narrow signal — a response CREATED after our
  announce went out and not yet finished, plausibly the model speaking this very
  announcement.

They **stack**, because they answer different questions and sharing a budget
would let a monologue consume the grace that stops the buddy speaking over
itself. So at `fallbackMs = 6000` the announcer's worst case is 5 intervals —
**30s**, not the 12s an earlier reading of this assumed. The deadlock argument
survives that number without calling 30s tolerable: the owner-speaking leg is
taken only while the owner IS speaking, so it extends the buddy's wait, not the
owner's silence, and an owner who stops talking stops that leg at once. At most
one unspent deferral lands between their silence and the speech, which bounds the
silence anyone can be left in **waiting for a refusal** at 12s.

The browser voice is watchdogged too (`speakingBudget` = a 30s floor plus 140ms
per character, deliberately slower than any real voice): `speechSynthesis` can
drop an utterance without firing `onend` OR `onerror`, and `speaking` is what
`pending()` and `anchorPending()` count, so without a bound the false-reject half
is an **unbounded mute**. Two residuals sat on that watchdog and both are now
closed — read them as what the mechanism does, not as what it used to miss:

- **#996 — the watchdog RELEASES the notice.** It used to call `stopSpeaking()`
  only: the gates reopened (its stated scope, done correctly) but nothing was
  released, so on the exact event it exists to recover from the ids stayed in
  `inFlight` for the life of the page — never acked, never released, never
  re-announced. Since #970 that is not data loss (nothing announced is
  cursor-past, so a reload recovers it) but a permanently-`inFlight` id wedges
  the contiguity walk, so everything after it is spoken and never acked and a
  reload *repeats* it: suppression until reload, then duplicates. It now fires
  `onNotSpoken`, which is the same path a `speechSynthesis` `onerror` takes, so
  the next gated tick says it again. The false-reject half — a slow but LIVE
  utterance declared dropped — costs more than "a second telling":
  `stopSpeaking` empties `speaking` but cannot cancel the browser's audio, so
  the re-announcement pumps while the first utterance is still playing and the
  owner hears it **twice, simultaneously**, for whatever is left of it. That
  reopens #997 for exactly that window, deliberately — overlapping speech the
  owner can still parse beats a notice they never get — and what keeps the
  window rare is the budget measured 2.6-4.5x conservative. Double-speak is
  still the cheap failure here, as it is for `carriedTheReason`.
  And exactly one outcome per utterance: `onend`, `onerror`
  and the watchdog all route through one latch, or the watchdog's release and a
  late `onend`'s ack would both land and the notice would be said twice and
  acked once.
- **#997 — `pump()` defers to the browser voice, BOUNDED.** The budget gates the
  *notifier's* gates and never the announcer's own FIFO, so a second `must_speak`
  item queued behind a long notice was promoted in the same tick `armFallback`
  started the audio, and its `response.create` went out over it — two voices, at
  any watchdog length, reached without the watchdog firing at all.
  `pump()` now holds while `speaking` is non-empty and promotes the moment the
  audio ends. **The bound is the trade**: an unbounded defer converts an
  audio-quality defect into a suppression defect, which in a screenless channel
  is strictly worse. It is the `speakingBudget` of the utterance in flight — not
  a new constant — because that is exactly how long the page is willing to
  believe the browser voice is talking; past it the #996 watchdog has already
  declared the utterance dropped and pumped, and the timer is the backstop that
  **promotes rather than staying mute** if it somehow has not. So the false-accept
  half is a wait behind audio the owner is currently hearing, and the false-reject
  half is the two voices this closes.

The other half of the timer is `onNotSpoken`, and since #996 it has **two callers
carrying different kinds of evidence**. `speechSynthesis`'s own `onerror` is a
*positive report* that the utterance failed — before it existed the page merely
logged that error, so an announcement demonstrably not spoken was also never
released: its id sat in the notifier's map and suppressed every later tick for
the rest of the session. The speaking watchdog is an *inference* from the
absence of any end event past a budget deliberately slower than any real voice —
a guess, made in one direction on purpose, about an utterance that has already
outlived the longest time this page will believe a voice is talking. Both leave
a state indistinguishable from success unless something says otherwise, which is
all this handler needs. A *throw* from `speak()` is the third state and is
deliberately not routed here — it means we cannot know, and "assume heard" is
the safe reading, because claiming not-spoken would replay a notice the owner
may well have heard.

### Success must not over-claim either

`agentwire msg send` **queues** — delivery happens at the recipient's next safe
boundary and can defer behind the box gates. From the owner's ear, "I told the
orchestrator" followed by nothing is indistinguishable from a silent refusal and
**worse**, because success was affirmatively claimed. So the buddy says
**"queued — it'll land when that session is free"**, never "sent" and never
"done".

### Attribution

`--from buddy` alone is not enough: a recipient must tell a buddy-originated
request from a human-typed one **without reading carefully**, because the whole
point is that the human was not typing. The failure to prevent is not a *wrong*
message — it is an orchestrator acting on instructions the human never gave, or
acting twice.

Since **Slice 1b (#985)** attribution lives in the **kind slot**:

```
[MSG from buddy · voice] restart the portal ┃ said: "can you tell the
orchestrator to restart the portal" ┃ reply: agentwire msg send --to buddy
--kind done "<answer>" ┃ #a1b2c3  ⟨#f3a9c1⟩
```

(One line in reality — wrapped here to fit the page.)

**The `said:` slot carries the REQUEST utterance, never the approving one
(#953).** The approving utterance is `confirm <nonce>` by construction, so a body
built from it shipped the nonce into the recipient's scrollback on every approved
write and carried none of the paraphrase-check content §4b built the slot for.
The request utterance is the newest complete ring entry at propose time — the
sentence that asked for the message, spoken BEFORE this proposal's nonce existed,
so it cannot contain it by construction. One selection rule guards the remaining
path: an entry containing a confirm word is **skipped**, because a stale
`confirm <word>` from a prior proposal (wrong-nonce, expired, retried) can sit
newest in the ring and is not a request. Skipping falls back to the next-newest
entry, and an empty result **drops the slot entirely** — a slot whose expected
content is empty must not survive — so the false-reject half costs a missing
annotation, never a blocked or garbled write. `build_argv()` takes no parameters
at all, which makes "the approving utterance never reaches the body" structural
rather than a calling convention.

This resolves an internal tension the page used to hold both halves of: §4b
argues the nonce must be structurally unreachable from any echo-able channel,
while the example above showed it shipped to a terminal.

**The reply-path slot** (`reply: agentwire msg send --to buddy --kind done
"<answer>"`) rides in every body that fits. #962's live failure: the recipient
answered a buddy request IN ITS OWN TERMINAL and the reply never came back — the
owner is listening, not watching that pane, so an on-screen answer is a lost one.
`--from buddy` and the `· voice` kind slot say who asked; neither says how to
answer. This does, as a runnable command rather than prose, because the recipient
is an agent and the one thing it reliably does with a command is run it. It is
**droppable, whole-or-not-at-all**: it slots in before the id (so the id never
pays for it) and rides only when the full body still fits `MAX_BODY_CHARS`. Both
halves priced — included, the reply path is runnable; dropped, the cost is a
missing nudge and the role text still states the etiquette, never a
half-truncated command or a clipped id. The persona is told the slot is
conditional for exactly this reason: stated unconditionally, the buddy could tell
the owner a recipient was told how to answer when the slot was dropped.

The `--to` in that nudge is read out of the **frozen `--from`**, so it can never
name anyone other than the identity the message actually goes out under.

**How this moved, and what moved with it.** Slice 1 shipped attribution as a
`<voice>` marker at the **front of the body**, explicitly as a stand-in: with
`--kind request` the kind slot distinguished nothing, so the only prefix-level
distinguisher left would have been exactly the sender string §4 rejects, and the
front of the body was the same on-screen position the kind slot would occupy.
Slice 1b puts it in the slot for real. `inbox.Message.render` prints
`[MSG from buddy · voice]`, so the distinguisher is now the same field
`ESCALATE_KINDS` and the drain read — one thing, not two that can disagree. **No
marker remains in the body**; a body still carrying one is stale text, not
attribution.

Two things travelled with the move and are easy to miss:

- **The leading-dash guarantee was load-bearing and is now explicit.** The body
  reaches the CLI as a positional, and `instruction` is model-supplied, so a body
  opening with `-` is parsed as a flag — a bug this repo has shipped twice. The
  marker used to make that impossible for free by occupying first position.
  `confirm._lead_safe` now owns it deliberately, and `render_body`'s assertion
  checks that one mechanism rather than an accident of layout.
- **The body caps were re-measured and deliberately did not move.** Removing
  `<voice> ` returns 8 chars to the body budget, and `request` → `voice` takes 2
  chars off the rendered prefix; the worst rendered line falls 365 → 363 against
  the measured 520-char wedge boundary, so the pane measurement behind
  `MAX_BODY_CHARS = 300` still holds with slightly more margin than before. The
  freed 8 chars are spent where #981 finding 6 says they compete: the droppable
  reply nudge now survives in bodies that previously lost it.

The kind ruling itself — active, escalatable, **not** an interrupt — lives with
the kind table in
[messaging.md](sessions/messaging.md#ruling-voice-is-active-and-escalatable-and-is-not-an-interrupt-985),
next to the enum it constrains.

The verbatim REQUEST utterance rides along free, because the gate already had to
capture it. A recipient can always answer "did a human really say this, and in
what words", and can see it when the buddy mis-paraphrased — subject to the
`said:` caveat in the guarantee above: it is evidence of what was heard, not
proof of what was said.

### A refusal may not claim more certainty than the success it points at

The rule that generalises furthest out of this work, and it is cheap to check.

`dispatch_failed` said *"…so nothing was sent. Ask me again."* That reads as
helpful and it is a **definite claim the system cannot verify**:
`run_agentwire_cmd` reports `success: False` on `subprocess.TimeoutExpired`, and
a timed-out CLI may already have enqueued. Worse, pairing false certainty with
"ask me again" invites a re-propose that **double-delivers** — the acting-twice
failure, arrived at through a spoken line asserting more than the system knows.

Sweeping for that shape found a second one that had been there longer, and it
is the clearer illustration: **`replayed` said "I already sent that one" while
the success path it refers back to says "queued"**. Those cannot both be right.
The success line is careful precisely because `msg send` queues; a refusal
pointing back at it inherited none of that care.

So, as a rule:

> **A refusal may not claim more certainty than the success it points at.**

Applied here: `dispatch_failed` names the uncertainty *and* the next move — and
note the next move **changes because of** the uncertainty. "Check that session
before asking me again" is verify-then-decide, not re-propose. That instruction
is only reachable by admitting what is unknown; stating the uncertainty made the
advice better, not vaguer.

**And it cuts both ways — spoken lines and behaviour can each falsify the
other.**

- **A rewording for honesty can silently drop a taxonomy property.** Rewriting
  `replayed` to stop saying "sent" removed its stand-down cue; the only reason
  it did not ship that way is a test asserting every outcome names the owner's
  next move. If you reword a spoken line for accuracy, re-check the property it
  was carrying.
- **A change in behaviour can silently falsify a sentence elsewhere.** Making
  `wait` deny turned *"You said no, so I haven't sent it"* into a false
  statement — the owner said *"wait for the tests"*, not "no". Nothing about that
  line changed; the policy underneath it did. Same shape as `replayed` claiming
  "sent" against a success path that says "queued". (The line now reads *"I heard
  you hold off"*, and the outcome table above carried the old reading for a round
  after the line itself was fixed — the same defect, one document over.)

So the check runs in both directions: **after changing a spoken line, re-check
its properties; after changing behaviour, re-read every line that describes
it.** Neither edit looks like it touches the other, which is exactly why both
need saying.

### The buddy's own voice cannot deny (#992)

`speechSynthesis` is outside the WebRTC path, so `echoCancellation` does not
cover the fallback voice: what it says re-enters the microphone and lands in the
**USER** transcript. Approval by echo is closed structurally — the fallback
channel never carries a nonce (#953). `carries_denial` has no such gate, and
several `SPOKEN` lines contain denial triggers: *"I heard you **hold off**…"*,
*"**Hang on** — I'm already working on that one"*, *"I **don't** have anything
pending…"*. Echoed inside the approval→confirm window, one of those retroactively
denies the owner's own approval and reports a take-back they never spoke.

**The rule is CONTENT, not timing, and that choice is the whole decision.** The
obvious rule — "utterances transcribed while the fallback voice is speaking do
not count as denials" — is unusable here: barge-in over the robot voice is the
NORMAL way to retract in this channel, so that rule drops genuine take-backs and
the write goes out. That is the acting-twice direction, strictly worse than the
wrongful refusal it fixes, which costs one re-spoken approval (the newest-first
binding in `_judge` makes recovery cheap). A content rule has no such half: it
cannot fire on words the buddy never said.

`confirm.is_buddy_echo` therefore asks whether the WHOLE normalized utterance is
a contiguous run of tokens inside some `SPOKEN` line, at least
`_ECHO_MIN_TOKENS` (6) long. Both properties are load-bearing:

- **Whole-utterance.** A barge-in captured together with the tail of the buddy's
  line (*"hang on im already working on that one no stop"*) is not a contiguous
  run of any line, so it denies. Only a clean capture of the machine's own words
  is suppressed.
- **Six tokens.** That is the entire false-reject budget, chosen against the
  collision rather than for tidiness: the triggers in those lines are exactly
  what a real owner says (*"hold off"*, *"hang on"*), so a two- or three-token
  floor would suppress a genuine retraction — and a suppressed retraction
  WRITES. A short or garbled echo misses the floor and denies: the enumeration
  **fails closed**, and nothing about being incomplete here can make a write
  happen.

It is applied at **two** sites, and the second is easy to miss: `_judge`'s own
newest-first loop reaches an echo that landed after the approval *before* the
post-approval scan does, so guarding only the scan leaves the loop returning
`denied` first. (Only echoes containing a confirm word reach that branch —
`classify` returns `NO_MATCH` before consulting the denial grammar otherwise —
which is why a test built on the wrong line passes with the loop guard deleted.)

**What it does not cover, stated rather than implied.** `SPOKEN` is this
module's own taxonomy, so the rule covers only what the confirm spine makes the
buddy say. Attacker-influenceable text — a delivered message body, spoken
verbatim by `composeNotice` — is not in it. That text cannot reach this window
today for a separate reason: `client.py`'s `canSpeak` **and** `canInterrupt`
both require `!confirmGate.outstanding()`, and the gate is closed from the moment
a proposal is spoken until its terminal outcome or TTL — which is exactly the
approval→confirm window. **If that gate is ever relaxed, this rule needs the
spoken text fed to it** rather than read from `SPOKEN`; the
"said-during-fallback" mark stays the wrong instrument for the reason above.

### Denial words and denial EXCEPTIONS have opposite bars

Both are one grammar and the instinct that serves one betrays the other.

- **Denial words: prefer tight.** A missed denial is recoverable — the write
  still needs a nonce, so the owner can simply not say it. Over-broad words are
  the expensive direction: `not`/`never`/`hold`/`forget` turned *"confirm tango,
  it is not urgent"* into *"You said no."*
- **Exceptions: prefer few.** An exception SUPPRESSES a denial, so a wrong one
  means **the owner said no and the write went** — which they cannot undo by
  declining to speak, because they already spoke and it did not count.

The bare words are recovered as **ordered bigrams**, and order is the whole
point: *"hold on"* denies, *"on hold"* does not, which is the precise instrument
for "confirm tango, the worker is on hold". Three bigrams were audited out
against the closed-phrase test below, each measured DENYING a real approval:
`("not","that")` ("it is not that urgent" denied while "it is not urgent"
approved — a flip on one added word), `("back","off")` ("back off the throttle
after"), and bare `cancelled`/`canceled` ("the other task cancelled" — ordinary
past tense about something else).

**Fillers are stripped before any of this matches**, rather than tolerated
per-rule. "hold, uh, on" is the same retraction, and handling that rule-by-rule
is how one rule ends up forgetting. That enumeration is on the safe side by
construction: skipping a filler can only make a denial EASIER to match, so an
unlisted filler fails CLOSED.

#### `("never", "confirm")` — a gapped pair, and the one place the fallback fails

`never` is excluded from the word list for good reason: it is among the commonest
words in English. The general argument for tolerating that exclusion is "the
write still needs a nonce, so the owner can simply not say it" — and **that
argument fails on exactly one utterance.** *"Never confirm tango"* IS the
retraction and it CONTAINS the nonce, so the fallback the exclusion leans on is
the very thing being spoken. Measured before the fix: APPROVED, along with "you
should never confirm tango".

So it is a **gapped** ordered pair — the two tokens with a bounded run of
tolerated words between them. "Adjacent" was already a fiction: fillers are
stripped before matching, so every entry in this grammar has always been
"adjacent modulo a skip set", which is why *"never, uh, confirm tango"* denied
while *"never ever confirm tango"* approved. Shipping the pair as strictly
adjacent stated a rule the matcher did not implement, and the gap it left was the
commonest intensifier in the language.

The gap set is a **closed class — degree adverbs, nothing else.** An open gap
("any word between never and confirm") would deny *"I would never send that
without checking — confirm tango"*, which is an approval. And this enumeration
**sits on the fail-open side and cannot be moved off it**: an unlisted gap word
ends the run and the utterance approves. What bounds it is that the class is
closed and small; what does not bound it is anything structural. Stated rather
than implied.

Two details a reader will otherwise assume wrongly. The pair is deliberately NOT
also in the ordinary bigram set — two spellings of one rule drift apart, and the
zero-gap case is just this rule with an empty run. And the second half is
`confirm` **alone**, not the confirm-word tuple: *"never confirmed tango"*
approves, and should — the past tense is a statement about what happened, not an
imperative retraction. Same exact-token reasoning that keeps `waiting`/`waited`
out of the `wait` rule.

`("wait", "for")` is the worked example, and it was tried **twice** before being
removed. First unconditionally, which swallowed *"wait for it"* — an idiom
meaning exactly "hold on". Then conditionally, guarded by "suppress only when a
real object follows", which failed in **both** directions: *"wait for those /
these / mine / both / everything"* approved (holds, so the write went out) while
*"wait for that build"* denied (a real condition). The comment described a
determiner/noun rule; the code was a 17-entry hold-word denylist, three lines
below a comment saying denylists were the thing being avoided.

**And inverting it does not rescue it.** The obvious repair — default deny,
suppress only on determiner + noun — cannot work, because *"wait for a second"*
(a hold) and *"wait for a build"* (a condition) are **structurally identical**.
No structural test separates them, so the only remaining instrument is a list of
time-unit nouns, and *that* list's incompleteness fails open.

Which gives the rule that sharpens the filler-denylist lesson this file already
carries:

> **When a set must be enumerated, enumerate the side whose incompleteness is
> safe.** An incomplete list of words-meaning-HOLD fails open — an unlisted hold
> word approves a retraction. An incomplete list of structures-meaning-CONDITION
> fails closed — an unrecognized phrase denies an approval, costing a re-propose
> and nothing else.
>
> **And a set may be enumerated safely at all only if it is a CLOSED PHRASE
> rather than an open class** — a closed phrase has no next word to have missed,
> so its incompleteness cannot fail open. That is the test any new exception has
> to pass.

Those two sentences are one rule, and the second is the half you need when you
want to ADD something: the first explains why `_BARE_DEICTICS` had to go, and
only the second explains why `("dont", "forget")` gets to stay.

The intuition is that "don't forget X" has no reading meaning "cancel". That is
arguable. **The checkable reason is better, and it is the form to argue a new
exception in:** an exception suppresses **exactly the tokens of its own span** —
here the `dont` and the `forget`, two tokens for a pair and three for a trio —
and cannot mask a denial signal anywhere else, because the word loop continues
past it and the bigram loop has already run. Its incompleteness has nothing to be
incomplete *about*. A list of *hold words* can never make that claim: each entry
masks an open-ended class of utterances.

> **That sentence was false in the code for one round, and it is the whole safety
> argument.** The masking loop computed `len(trio or pair)`, and `trio` is
> non-empty whenever any token remains — so a matched TWO-token exception masked
> THREE and ate the word after its own span. Measured: *"confirm tango, don't
> forget — hold on"* and *"confirm tango, don't forget, cancel the other one"*
> both APPROVED, and `carries_denial("don't forget, wait")` was False, so the
> post-approval scan was blind to it too. **An exception's mask is only ever as
> safe as its length**, which is why the span is now taken from the rule that
> actually matched — and why the wording this paragraph retired,
> "exactly one token", is the wrong thing to argue a new exception against.

The uncontracted twin is listed separately as a trigram — `("do","not","forget")`
— because normalization does not merge the two forms. That is the same
reachability trap that made this grammar dead once already: `donot` and
`nevermind` were carefully written entries with no path into them, because speech
transcribes as "do not" and "never mind". **Testing a table's entries against
themselves proves the table, not the path into it**, so the tests for this drive
the real pipeline — raw utterance → normalize → classify — and never the matcher
in isolation.

**`("cant", "wait")` is the second exception and clears the same bar.** It is a
closed idiom — "can't wait" has no reading meaning "hold off" — and it was a
measured false reject: *"confirm tango, tell them I can't wait to see it"* DENIED
on the bare `wait`. Post-normalization `cant` is a distinct token, so the pair is
expressible without touching `wait` itself, and the mask covers those two tokens
only: any other retraction in the utterance still denies, **including a second
bare `wait`**. Its price, stated rather than assumed: a hesitated hold spelled
*"can't — wait!"* normalizes to the same two tokens and is suppressed. That is a
real false accept, accepted for the same reason the `("hold","on")` ordering is —
the idiom is common in ordinary speech and the hold spelling is rare.

Note the anchor sits at the TAIL there (`wait`), not the head. An exception must
CONTAIN a denial trigger or it suppresses nothing; requiring it first would be a
rule about spelling rather than about what is being suppressed.

The problem was never enumeration as such. It was that this enumeration sat on
the side where being wrong **writes**. So there is deliberately **no CONDITIONAL
exception**, and `wait` denies wherever the closed `("cant", "wait")` idiom does
not mask it.

**And denying on `wait` turned out to be correct behaviour, not a tolerated false
reject** — for a reason neither the rule nor the cost model reaches. The write is
`msg send` and it fires **immediately**; the buddy has no defer mechanism at
all. Approving *"confirm tango, wait until you hear back from the reviewer"*
would **send now** while the owner believes it is being held — a silent
divergence between what they said and what happened, strictly worse than a
re-propose. A "wait" clause attached to an approval is **semantically
unhonorable**. Its correct home is the *instruction*, frozen at propose ("tell
the reviewer to wait until X"), where it is content for the recipient rather
than a condition on the send.

The cost is smaller than it looks, too: matching is on the exact token, so
`waiting` / `waited` / `awaiting` never fire — only the bare imperative does.

**This is correct only while recovery is cheap.** Composed with a binding bug
that made retractions permanent, it produced a *dead proposal*: the owner's
natural recovery — saying the phrase cleanly — failed and kept failing to the
TTL, and the `denied` line promising "say the phrase again" was thereby false.
Deliberate denials and cheap recovery are one design, not two.

**Corollary for anything guarding a set like this: assert the property, not the
cardinality.** Counting the exceptions would not have caught this — the old
design had three, and the danger lived in a seventeen-entry list the count never
covered.

### Method note: a signal being produced is not a signal

Not a voice-layer lesson. It bit in **three different tools on one afternoon**,
and all three are the same mistake.

| Where | What happened | What it looked like |
|---|---|---|
| A test-count watcher | `until grep -q "N passed" <file>` ran against a file **pytest was still writing** — it matched a partially-flushed buffer, or a previous run's content in a reused path | a confident count belonging to a *different run* |
| A CI watcher | The loop waited while any check was `PENDING`/`IN_PROGRESS`. `QUEUED` was not in that list, so it fired on a run **that had not started** | "CI is settled" |
| A CI result | A job failed with *"Set up job: Failed to resolve action download info, Service Unavailable"* — it **never ran** | a red X that reads as a test failure |

**Treating a signal as settled while it is still being produced.** The third is
the sharpest, because that red carries **no test signal in either direction** —
it is not evidence of failure *or* of pass, and reading it as either is wrong.

Two practices fall out of it, both cheap:

- **Wait for a completion signal, then read once.** Never poll a file that is
  still being written. If the tool gives you a "task finished" event, that is
  the read barrier; use it.
- **Enumerate the DONE states, not the not-done ones.** "Wait while
  `PENDING`/`IN_PROGRESS`" is an open-ended list — the state you forgot means
  you stop early. "Wait until everything is `SUCCESS`/`FAILURE`/`SKIPPED`/…" is
  closed. *(This is the same fail-open/fail-closed asymmetry as the denial
  grammar above, in a different tool. It generalises.)*

And the thing that makes it hard to catch: **reconciling two readings that came
from the same broken mechanism is not reconciliation — it is a coin flip with
extra steps.** Two disagreeing numbers from one racy watcher feel like evidence
about the *subject*; they are evidence about the *watcher*. When two readings
disagree, suspect the instrument before the measurement.

#### Why "local green is not CI green" is a better rule than its own rationale

That bar is usually justified as "the environments might differ" — and they
might. But the deeper reason showed up here: **a local count can come from an
unreliable measurement method**, and the bar forces a second independent source
regardless of whether the environments differ at all. The value was never the
comparison. It was refusing to trust a single source.

### Maintenance note: SPOKEN strings need tests, and they are the ones that don't have them

A pattern worth knowing before you add a sentence the buddy says out loud.

On this feature the code paths accumulated tests naturally and **the spoken
paths kept not having them** — because a wrong sentence and a right sentence are
*structurally identical at review time*. Both are a string literal in the right
place. Nothing about a stale one looks stale.

It has bitten twice already, and the second time was bigger than expected:

- Two strings survived the nonce alphabet changing from digits to words — the
  scripted `say` field in `write_tools` (*"Say the two digits separately"*) and
  the persona's own example (*"to approve, say confirm four seven"*). Both would
  have been spoken on **every proposal**. Grepping for `digit` would only ever
  have found the one that used the word.
- A later sweep found **six more**: the client's own `announce()` literals for
  bridge-unreachable, channel-closed, garbled-data, tool-failure, service-error
  and dispatch-failure. Not one was asserted anywhere.

Scripted instructions are a *mechanism* — the whole reason they exist is that a
specific turn says specific text. A wrong script is not cosmetic there; it is
the mechanism working exactly as designed, with the wrong content.

**So there is a set, and it is asserted.** If you add a spoken string, add it:

| Surface | Guarded by |
|---|---|
| `confirm.SPOKEN` (per-outcome refusals) | `REASONS` ↔ `SPOKEN` **both ways**, plus a reachability test |
| `Verdict.to_dict()["say"]` (success) | asserts "queued", forbids "sent" |
| `write_tools` scripted `say` | asserts the live phrase, forbids digit-era wording |
| `instructions.build_instructions()` | asserts the real alphabet appears |
| `client.py` `announce()` literals | the set is **pinned**; each checked for speakability |

The one-directional guard is worth calling out on its own: checking "every
outcome has a line" catches a mute refusal but lets **a line without an outcome**
ship as dead code — which is exactly how `too_many_attempts` shipped a carefully
written sentence with no producer, while the attempt that really retired a
proposal told the owner to repeat a phrase that had just stopped working.

### Two ways to draw a unique nonce, and why the obvious one is wrong

Uniqueness among live proposals is what closes "one approval, two proposals".
There are two ways to get it and they look equivalent:

```python
# The obvious one. It is wrong.
nonce = choice(WORDS)
for _ in range(tries):
    if nonce not in taken: break
    nonce = choice(WORDS)
else:
    raise
```

With **k of n** words taken, that fails spuriously with probability
`(k/n) ** tries` — it refuses a legitimate proposal while a nonce is still
free. At 19 of 20 taken and 64 tries that is **3.8%**: rare enough to read as a
flake, frequent enough to happen. It shipped here, and it surfaced exactly that
way — the exhaustion test passed on its own and failed once in the full suite.

Draw from the free set instead. Then exhaustion is a hard error and
near-exhaustion is a non-event:

```python
free = [w for w in WORDS if w not in taken]
if not free:
    raise RuntimeError("no free nonce — reusing one would let one approval satisfy two")
nonce = choice(free)
```

`confirm.mint_nonce(taken)` is the only minting path, for the same reason: a
second, subtly-different way to do this sitting next to the right one is how
the wrong one gets called later.

### The delivery cliff this cap defends against

Measured, and it turned out to be a fact about the **messaging subsystem**
rather than about voice — so it lives in its own section at the end of this
page: [Appendix — the #689 heal line-count cliff](#appendix--the-689-heal-line-count-cliff-930).
Read it before changing anything about message length or the drain; it is
written for whoever implements #930, not for this feature.

### What the voice layer's cap does and does not buy

`MAX_BODY_CHARS = 300` and `MEASURED_STUCK_LIMIT_CHARS = 520` live in the voice
layer as a **caller-side** cap. Making the cap a property of `msg` itself is the
right long-term shape (#930) and is deliberately not done here: it changes
behaviour for every sender in a shared subsystem, which is the same class of
change as the `voice` kind Slice 1b shipped (#985), and it deserves its own
review.

**The residual, stated rather than implied.** The one-line rule protects the
**single-message case**: a lone voice write is well inside both regimes. State
the cap, not a measurement of it — the older numbers here (a 279-char body) were
taken before the reply nudge existed and the nudge now fills toward the cap, so a
measured figure goes stale on the next slot that fits. The bound is
`MAX_BODY_CHARS = 300`; the delivered line adds the `[MSG from <sender> · <kind>]`
prefix and the `⟨#id6⟩` tail, so a 32-character worktree sender name lands the
worst case at 363 against a measured 520 and a 4-line cliff. That derivation
holds however the slots inside the body are rearranged. It does **not** protect a
voice write that is coalesced behind other
messages — that is #930, it is governed by a variable the voice layer cannot
observe, and no per-caller fix can bound it. Do not read the cap as "one line,
so the heal fires."

### Why backticks and quotes in a body are safe — and how that could silently stop being true

Hops 1 through 3 are clean, measured: the bridge builds a **list argv**
(`subprocess.run(["agentwire", *args])`, no `shell=True`), the inbox round-trips
through `json.dumps`/`loads`, and the tmux buffer path carries backticks,
`$(…)`, quotes, semicolons and backslashes intact. **That safety is a property
of using list argv, not of the content being harmless** — refactoring any hop to
build a shell string would silently reintroduce evaluation, and the body is
model-supplied. Same hazard class as the control characters below: content that
is fine as DATA becoming active in a layer that evaluates it.

### Control characters, which are the same failure reached by rewriting

`\s+` does not cover them. It catches tab, newline, CR, FF and VT; ESC, BEL, SOH
and friends pass straight through. Measured against real tmux: a body carrying an
ANSI escape or a BEL renders into the pane as an invisible control **action**, so
`capture-pane` returns text that no longer contains the rendered needle,
`flush_session`'s `stuck` substring test misses, the #689 heal never fires,
`_box_static` classifies it no-penalty, and the message is **permanently wedged:
never healed, never dead-lettered, therefore never emailed.** That is the same
outcome newlines cause, arrived at through character rewriting rather than
layout.

The realistic carrier is **not** the transcript — a speech-to-text model does not
emit ESC — it is `instruction`, which is model-supplied and was only
length-bounded. So `strip_controls` runs at **both** ends: at propose time before
the argv is frozen, so the frozen argv is clean by construction and "frozen"
still means what it claims, and again in `render_body`. It costs nothing in
verbatim fidelity (no human utterance contains ESC) and it is deliberately narrow
— the round-trip test asserts that smart quotes, em-dashes, accents and emoji all
survive.

### The body cap is measured, not chosen

`tools/voice_heal_probe.py` pastes a real rendered message into a real Claude
Code pane and runs the actual heal. At 80x24, by rendered-line length:

| Rendered line | Box holds | `stuck` test |
|---|---|---|
| 470 | 482 | hit ✓ |
| 500 | 512 | hit ✓ |
| 520 | 532 | hit ✓ — last passing |
| 540 | 480 | **miss** — the box starts windowing |
| 880 | 16 | **miss** — `[Pasted text …]` chip |

**There are two failure regimes above the boundary, not one.** The box windows
first, and only much later collapses to the chip — so "it isn't a chip" is not
evidence the heal will fire. `MAX_BODY_CHARS = 300` puts the worst case
(a maxed body plus a 32-character worktree sender name) at 363 against a measured
520. It was 365 while the kind slot read `request`; Slice 1b's shorter `voice`
moved it down by 2, which is why re-measuring after that change was owed and why
the cap itself did not need to move.

Re-run at 80x24 on **2026-08-11** for #1015, with the relay pointer riding: 476
hits, 546 misses (box 480), 1026 chips. The recorded 520/540 boundary sits inside
that window, so it stands — and the shipped worst case (336 chars rendered) closed
the round trip whole.

The measurement is **pane-dependent**: the box shows a bounded number of rows,
so a shorter pane windows sooner. Do not spend the headroom without
re-measuring at the smallest pane you care about.

The round trip itself is closed, live: paste → text lands → `stuck` hits →
`finish_submit` submits → the dedup finds it on scrollback. `VERIFY_SCROLLBACK_LINES
= 200` is not the binding constraint at these lengths (520 chars is ~7 rows).

**One line, capped. Newlines are unsafe**, and the reason is not the one you
expect. The paste is fine (bracketed paste, `enter=False`) and the #621 dedup is
fine (it whitespace-normalizes both sides). **The #689 heal is what breaks:** a
multi-line paste renders as the `[Pasted text #N +M lines]` chip and nothing
else, so `flush_session`'s `stuck` substring test finds nothing, `finish_submit`
never runs, `_box_static` classifies it no-penalty after three sweeps, and the
message is **permanently wedged — never healed, never dead-lettered, therefore
never emailed**, surfacing only via `doctor` after two hours. For a channel whose
entire justification is "the owner is not watching a screen", that is the worst
available failure. The same `stuck` test has no #851 window path, so an
over-long single line fails identically — hence the cap.

### The inline text is a preview; the file is the message (#1015)

The cap above is not raisable, and for three live relays in the first voice
session that meant the recipient got `"Treat it as a running list for anyt…"` and
acted on the fragment. **A cap you cannot raise is not an argument for losing the
text** — the pane is one channel, the filesystem is another, and the recipient is
an agent that reads files.

So every buddy write now persists the WHOLE request to
`~/.agentwire/voice/relays/<proposal-id>.md`
(`agentwire/voice_layer/relay.py`) — full instruction, full verbatim utterance,
nothing clipped — and the delivered line carries a `full: <path>` slot pointing
at it. The same `--ref` rides on the `msg send` argv, the way `ingest` messages
already carry a report path. The inline text is demoted to what it always
actually was: a preview.

Four rulings hold it together:

- **The pointer is frozen at propose time; the file is written at execution.**
  The path is a pure function of the proposal id (`secrets.token_hex(3)`), so
  `--ref` can join the argv prefix while the file does not yet exist — "the whole
  argv is frozen at propose" (#966) stays literally true, and a proposal the
  owner cancels or lets expire leaves nothing on disk. `build_argv` matches the
  frozen `--ref` against the path the id *derives*, **at the tail** — `propose`
  appends its pair last, so the tail is the only position that identifies *our*
  pair. Reading "the first `--ref`" would let a future spec's own frozen `--ref`
  steer the write, and on the mismatch would leave our pair in place naming a
  file nothing wrote (the dangling pointer this section calls worse than none)
  while deleting the other spec's pair instead of ours. Tail-matching makes all
  three unreachable.
- **The file is written owner-only.** It holds the owner's verbatim speech, so it
  goes through `core.write_owner_only` (0600, mode set on the descriptor before
  any bytes land) — the ONE implementation #887 established after the third
  hand-rolled temp-and-replace shipped a 0644 file. That also owns the temp
  file's lifetime, where a fixed `.md.tmp` name would orphan a file the `*.md`
  prune glob can never collect.
- **Writing never raises.** `write_relay` is called from `build_argv()`, which
  `_dispatch` runs *after* `_proposals.pop()` and *outside* the runner's `try` —
  exactly the position where `_lead_safe`'s raise once destroyed approved
  messages with no screen to report it on. A failed write returns `""`, and the
  caller drops the `--ref` **with** the slot: a pointer to a file that is not
  there is worse than no pointer, because the recipient reads a missing path as
  "the real instruction is elsewhere" and stops.
- **The pointer is not droppable, and the priority order is the ruling.** The
  reply nudge is droppable; this is not — it is the recoverability of everything
  the other slots clip, so dropping it under budget pressure would be dropping
  the fix, silently, on exactly the longest messages. When a deep `$HOME` makes
  the path long enough to squeeze the preview below `MIN_EXCERPT_CHARS`, what
  gives way first is the `said:` quote, because that quote is reproduced verbatim
  in the file the pointer names. Recoverable yields to unrecoverable. Only a path
  long enough that even that is not sufficient drops the pointer itself — a
  preview nobody can scan buys recoverability nobody goes looking for — and the
  quote then **comes back**, because a body carrying neither would be strictly
  worse than what shipped before this and buy nothing.
- **It rides exactly when something WAS clipped — asked WITHOUT the pointer's own
  cost.** That is the whole predicate, and getting it wrong is self-fulfilling:
  deducting the pointer first moves the budget 160 → 133 (90-char quote, 47-char
  path), so a 145-character instruction that rendered whole before #1015 would be
  clipped to 133 and handed a pointer to the text the pointer's own arrival
  clipped — a message made worse by the fix for messages being made worse. The
  question is "does this body lose anything as it ships today?", so it is asked of
  today's body. Anything that fits today is byte-identical to before, and the
  134..160 boundary is pinned in both directions (with a must-fail control, since
  a predicate that never fires would pass the first half alone).

The caps did not move: the pointer is paid for out of the excerpt and the
droppable nudge, so the worst rendered line is still 363 against the measured
520. What changed is that the excerpt's clip is now recoverable instead of final.

**Residual, stated.** `write_tools.MAX_INSTRUCTION_CHARS = 600` still refuses a
proposal longer than that outright. That refusal is *spoken* — the buddy says so
and the owner can restate — which is a different failure from silently handing a
session half a sentence, and it is not what #1015 was about. If real spoken
requests start hitting it, the fix is a bigger cap plus a bound on what the
announce reads back aloud, not a wider excerpt.

### The `voice` kind, and what the substitution dragged in

Slice 1 rode `--kind request` and deferred `voice` to **Slice 1b (#985)**
deliberately: it is the only part of this work that changes behaviour for
sessions with nothing to do with voice, it touches a shared subsystem's
escalation and dead-letter paths, and its blast radius is non-obvious. Both
halves shipped in 1b; the ruling that governs the kind lives with the enum in
[messaging.md](sessions/messaging.md#ruling-voice-is-active-and-escalatable-and-is-not-an-interrupt-985)
(active, escalatable, **not** an interrupt).

**The three hand-written tuples were the real hazard.** `doctor`'s dead-letter
section and both `worktree --list` / `--watch` each filtered their own
`("done", "escalation")` literal. Two consequences, one of which was already
live before voice existed:

- **That gap already bit `request`.** A dead-lettered `request` emailed the owner
  (it is in `ESCALATE_KINDS`) but was invisible to `doctor`'s `[!!]` line and to
  the `worktree --list` badge. Not a voice bug — a pre-existing disagreement
  between three copies and the SSOT.
- **Adding `voice` would have split them again**, this time on the one kind
  whose sender is screenless: a dead-lettered voice message emailing the owner
  on one path and vanishing on another.

So 1b deleted all three and routes them through **`inbox.load_bearing()`**, the
one derivation from `inbox.ESCALATE_KINDS`. A test feeds one message of every
kind through it and demands the survivors are set-equal to `ESCALATE_KINDS`, so a
future kind cannot escape the consolidation by being forgotten at a call site.

**The cohort interaction, which filters on two different fields.**
`inbox._cohort_held` holds by **sender**; `cohort._harvest` harvests by **kind**.
Under Slice 1 the buddy's write was a `request`, which IS in
`cohort.REPORT_KINDS` — so had the buddy ever been enrolled as a pending cohort
child of the recipient, `wait --children` would have harvested the buddy's write
as a child report and consumed it. `voice` is deliberately **not** in
`REPORT_KINDS` (the owner is not a child reporting on a task), which closes that
specific hazard and leaves a benign one in its place: a `voice` message from a
pending child is held by sender but never harvested, so it stays pending and
delivers once the cohort resolves — a deferral, not a loss, and the same shape
`ingest` already has. Both halves are pinned, in
`tests/unit/test_voice_body_delivery.py::TestTheCohortInteraction` and
`tests/unit/test_voice_kind.py::TestCohortInteraction`.

## Cold fleet: the buddy never starts an orchestrator

If no live session is listening, the buddy **says so out loud and stops**.
"Nothing is listening" is a correct and useful spoken answer.

The pull toward a bootstrap escape hatch — *let it start one orchestrator, just
this once, when nothing is live* — is real, and it is the design working. That
would be session-creation semantics through the back door, and "just this once"
is how the boundary in §2 dies. It is not built, and
`test_the_buddy_has_no_tool_that_starts_a_session` asserts its absence, because
that boundary dies quietly (a plausible tool gets added) rather than loudly.

**Clarification, because the "does not" list reads worse than the truth:** the
voice conversation itself is complete and works end to end — mic in, voice out,
barge-in, tool calls, spoken answers. Asking "what's the fleet doing" and getting
a spoken answer works today, and so does asking it to pass a message on.

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

- **The confirm gate is in CODE, not in the prompt — and this is the one place
  the spike's own advice was wrong.** The spike page used to say their
  anti-filler guardrail was "the part to copy most carefully". Read the source
  before copying it: `voice/instructions.ts` lines 42-46 is a *paragraph asking
  the model to be strict*, with a concrete false-positive list; `tools.ts` says
  so explicitly — *"Guardrail language against loose 'yeah'/filler agreement
  lives in instructions.ts, not here; these are just the callable surface"* —
  and their own file comment is blunter still: *"there's no code-level pattern
  match on 'yes' — the model itself must judge deliberateness"*. There is no
  mechanism. And the stated fallback for when the model gets it wrong is *"a
  missed verbal approval just means they tap the card"* — a click surface a
  voice-only user cannot reach (#748).

  **So the part we were told to copy most carefully is the part that never
  worked hands-free.** Their meta-tool shape (`confirm_pending_action` bound to
  a `call_id` from the proposing turn) is genuinely worth taking, and §4a's
  proposal binding is that shape. The judgment is not: here it is a nonce
  evaluated in code against the transcription model's output, and the approval
  surface is speech, so there is no card to tap.
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

The buddy is not an agent session, so it needs somewhere to live. The
[custom-services registry](services.md) is the natural home — autostart on
portal launch, watchdog health checks, restart with backoff. What that registry
had, until #983, was only *agent* services: every entry was an
`agentwire new` session, and there was nowhere to declare a process. So the
registry grew a second kind, `command`, which is entirely generic — it
supervises a process and has no idea the voice layer exists. The buddy is one
caller of it.

The entry, ready to paste. It is **not** written into the owner's
`config.yaml` by anything on this branch (that file is protected control plane,
and a spike must not add itself to a startup path); wiring it up is one edit:

```yaml
# ~/.agentwire/config.yaml
services:
  custom:
    - name: buddy
      command: agentwire buddy serve buddy --port 8788
      autostart: false        # flip to true when you want it on the startup path
      restart: on-failure
      healthcheck:
        kind: tmux_session
        interval: 60
```

Three details in that block are rulings, not defaults that happened to land:

**`kind: tmux_session`, not the `curl http://127.0.0.1:8788/` this page used to
suggest.** `/` is the route that hands over the run token — it is served with
no auth at all, which is the whole reason the `Host` allowlist runs first on
every route. A healthcheck polling it would pull a fresh copy of the token into
the watchdog's process once a minute, forever, to learn something tmux already
answers. A probe should not fetch a secret to find out whether a port is open.

What "the tmux session answers" is **not** simply "the session exists" — that
sentence was true only while nothing kept a dead pane around, and the buddy's
own spawn now deliberately does. `tmux has-session` returns 0 for a session
whose pane is a corpse, so the `tmux_session` healthcheck asks `#{pane_dead}`
as well. Without that second clause, retaining the crash reason would have
traded a false success at start for a permanent one.

**`autostart: false`.** The buddy needs `OPENAI_API_KEY` and a browser tab to
be useful; a machine that boots it and never opens the page has spent nothing
but a port. Still, opting in is the owner's call and doctor does not nag about
a service nobody asked to run — an `autostart: false` entry reports `[..]`, not
`[!!]`.

**Nothing is redirected to a log.** tmux captures the process's stdout into the
pane's scrollback, which lives in the tmux server's memory behind a 0700
per-user socket dir. Measured, not assumed: the pane holds exactly the two
lines `serve` prints, the token appears in neither, the HTTP server's request
log is a no-op, and the token is absent from the process table. What the
process table DOES expose is `command` itself, so a secret must never ride in a
service's argv — doctor flags one that looks like it does, in both the
`--token=x` and `--token x` joinings, since they reach `ps` identically.

### The failure the buddy hits first

`agentwire buddy serve <name>` with an unregistered name refuses and exits at
once — and with `autostart: false` shipped above, opting in is precisely when
the owner meets it. A supervisor that reported that as a successful start would
hand them a success line, a `[!!] unhealthy` from doctor a second later
prescribing the command that just claimed success, and no copy anywhere of the
process's own explanation. That is the shape this whole branch exists to
remove, so the command kind spawns with `remain-on-exit on`, re-reads the pane
after a grace period, and fails with the process's last lines attached:
`process exited immediately: No voice buddy named 'nope'.` The corpse is left
in place — tmux's memory is the only copy — and the next start reads it before
clearing it. Still no file, still 0700.

Those last lines are **redacted before they leave the pane**, because they do
not stay on a terminal: the healthcheck detail built from them is toasted by
the portal watchdog and spoken through `agentwire say`, so a bridge printing
`bearer eyJ…` on the way down would otherwise have had that read aloud. Same
pattern set as the argv check, value masked and the message kept — and it masks
a value that follows a key it recognises, nothing more; the exact boundary
(what it does not catch, and why that is a stopping point rather than a gap) is
tabulated in [services.md](services.md).

### Restart semantics: what a supervisor kill mid-handshake leaves behind

Nothing pending, and this is now pinned rather than asserted. The confirm spine
and the utterance ring are built per `BuddyBridge`, one bridge per `serve()`,
and neither `confirm.py` nor `transcript.py` touches disk — so a watchdog kill
between a proposal being anchored and its spoken nonce arriving leaves a
proposal that exists nowhere. `tests/unit/test_buddy_restart.py` asserts both
halves: the second run's spine and ring are empty and the dead run's token is
refused, AND nothing under `~/.agentwire` gained the proposal's token, nonce or
instruction. The second assertion is the one that survives someone deciding
proposals should be durable; the first alone would still pass if a restart
merely *reloaded* them.

One thing a restart does not undo, and it is worth being exact rather than
reassuring: a kill that lands *after* the write dispatched but *before* the
announcement is confirmed spoken leaves the write **sent and unannounced**. The
restart is clean; the fleet still got the message. That is a narrower guarantee
than "mid-handshake work is rolled back", and nothing here rolls anything back.

**The greet is the liveness probe.** Because the token is minted per run, a
restart invalidates the open tab — its POSTs 401 rather than half-working — so
the acceptance path is *reload, then Start talking*, and the greeting is what
says the whole approval path came back up (a heard greeting proves model audio,
which #950 made the write path fail-closed on). #995 recorded that the wires
arming that greet had no pin at all: cutting `maybeGreet()` out of `pc.ontrack`
left the entire suite green — **measured, 5736 unit tests, zero failures**.
`tests/unit/test_buddy_restart.py` executes both arming legs, extracted from the
page the server actually serves, and each cut now turns it red. The four other
wires #995 named — the data-channel `open`/`close` status wires and both button
click handlers — are pinned the same way in `tests/unit/test_client_wires.py`,
sharing one extractor (`tests/page_slice.py`) rather than a second idiom.

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

# pick a voice — see `--help` for the list, or the picker on the page
agentwire buddy register buddy --voice marin   # sticks; prints "updated voice → marin"
agentwire buddy serve buddy --voice ash        # just this run
```

The page carries a **voice picker** (#1017): changing it reconnects in one
click and persists the choice, because the API fixes a session's voice once the
model has spoken — see
[The voice list](#the-voice-list-and-why-switching-costs-a-reconnect-1017).

`buddy call` runs a tool through the same dispatch path the model reaches, with
**no spine wired**, so a write tool there is refused outright rather than
silently degraded to an ungated write — a caller that forgot the gate must fail
loudly. `buddy tools` prints the whole array, writes included.

### The loopback bind is NOT the guard. The Host allowlist is.

`serve` does bind `127.0.0.1` only, on a port that is never a portal port (8788 —
not 8765/8100/8101), and it does mint a fresh bearer token per run. **What this
page used to say next was wrong, and reading it that way is how the hole in #977
got here:** it said a tool-execution endpoint reachable from elsewhere on the
network is precisely the unguarded surface this design avoids, and presented the
bind as what prevents that.

Binding loopback does not make the bridge unreachable from the web, **because the
attacker never sends the packet — the owner's browser does.** A page on
`evil.com` that rebinds its own name to `127.0.0.1` becomes same-origin with the
bridge, fetches `/` (which is served with **no auth at all**), reads the token
embedded in the page, and POSTs `/tool` with it. Loopback is doing nothing
against that; every packet is genuinely local.

The one thing such a page cannot forge is `Host`: the browser sets it from the
address bar, so the rebound request says `evil.com` while the real client says a
loopback name. So the guard is an exact, case-folded allowlist —
`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`, plus the bare names when
the port is 80, since a browser omits a scheme-default port. Anything else,
**including a name that RESOLVES to loopback**, is foreign: resolution is
precisely what rebinding controls.

Three properties that are easy to get wrong and are each deliberate:

- **Checked FIRST on every route, GET included.** `/` is the request that hands
  the token over, so a guard running only on the authenticated POSTs would be
  checking the door after the key was taken.
- **The port comes from the LISTENING SOCKET, not the requested one.** `serve()`
  may be given port 0, and an allowlist built from the argument would then match
  nothing at all.
- **A missing `Host` refuses, and two `Host` headers refuse outright.** HTTP/1.0
  permits omitting it and nothing a browser does omits it, so refusing costs no
  real client while accepting it would make the guard bypassable by anything
  hand-rolling a request. With two headers, which one a proxy or parser believes
  IS the ambiguity, and no browser sends two.

**The false-reject half is the expensive one**, which is why the set is derived
from how the client actually connects rather than guessed: `client.py` fetches
every route as a RELATIVE path, so `Host` is always whatever is in the address
bar and never a value the page chooses. A refused local client is not an error
the owner reads — it is a buddy that stops working with no explanation.

**What this does not close, and is not claimed to:** on a multi-user machine any
other local user can `curl /` and take the token, because loopback is per-host,
not per-user.

`POST` bodies are clamped at **both** ends (`max(0, min(len, 64K))`). `min` alone
let a negative through, and `read(-1)` is read-to-EOF: the handler parked until
the client went away, with the request never having to send a body at all. **The
parked-thread class itself is NOT closed** — a request declaring
`Content-Length: 60000` and sending 2 bytes still parks a thread until the client
disconnects (measured: 5 requests, 5 parked threads, on the fixed code). Closing
that needs a read timeout on the connection, not a bound on the declared length.

---

## 8. TODO — next session picks up here

Ordered by risk, each its own reviewable diff. **Do not batch these.** The whole
point of the ordering is that each one can be rejected on its own.

### Open design questions — decide these BEFORE writing the code

These came out of reviewing the spike with the owner and are genuinely
undecided. A next session that picks an answer silently is the failure mode.

- [x] **Q1 — Where does the confirm tier live: in the voice model, or below
      it? SETTLED: below it, and the settlement splits in two.**
      **(a) Proposal binding** — a write tool refuses any call lacking a token
      minted on a prior turn, with the argv frozen at propose time, TTL-bounded,
      consumed on success. **(b) The approval judgment** — also below the model:
      a spoken nonce evaluated in code against the transcription model's output,
      never against the conversational model's claim to have heard a yes. (b) is
      the part DocumentScribe does not have; see §4a and the corrected §5.
      Shipped in Slice 1.
- [x] **Q2 — Is "spawn a worker" a tool, or a handoff to a real session?
      SETTLED: handoff.** The buddy composes a request and hands it to a session
      that already has damage-control hooks, posture and prompt routing. It
      never builds a `worktree_create` argv and never creates a session. This
      keeps the §2 boundary intact by construction rather than by discipline —
      and it means T1 adds no new authority when it lands, because under handoff
      "spawn a worker" is the same write with different words in the body.
      **The cold-fleet corollary is ruled and not open:** if nothing is
      listening, the buddy says so and stops. It never bootstraps an
      orchestrator (see Cold fleet).
- [x] **Q3 — What earns the right to interrupt? SETTLED (#967), and the answer
      is a routing rule, not a judgment.** Every candidate condition — a
      dead-lettered `done`, auth-expired, a usage-limit park, a blocked pane
      silently swallowing inbound (#905), a dangling PR — is something the
      fleet already detects and reports; the buddy does not re-derive that
      judgment, it keys on the **typed message kind** already in the inbox:
      `kind: escalation` is interrupt-class, everything else waits for a gap.
      The reconciliation with #962's never-barge-in: that rule splits into
      legs, and only one is relaxed. **Never while the owner is speaking**
      stays unconditional for every tier, and **never inside a confirm
      handshake** holds from the moment the proposal is QUEUED to its outcome
      or TTL — `confirmGate.outstanding()` measures only from the anchor, so
      `canInterrupt()` also requires `!announcer.anchorPending()` (#978 item 2);
      the announcer is the only thing that can see the window between the write
      tool returning and the announcement being confirmed spoken, and before
      that leg existed an escalation queued behind the proposal and was promoted
      the instant anchoring closed the gate.
      An escalation is allowed to skip only the "wait for the buddy's own
      chatter to finish" leg (`canInterrupt()` beside `canSpeak()` in the
      notifier). **What that buys is narrower than "pre-emption"**, and the
      overstatement is worth keeping corrected: `announce()` cancels an
      in-flight response, so pre-emption is real only against a **VAD**
      response. Against an ANNOUNCER item the escalation still QUEUES behind it,
      plus up to one 6s in-flight deferral and up to three owner-speaking ones —
      roughly 30s in the worst case. Escalation is the interrupt tier, and a
      promise of immediacy it does not have is exactly the sentence that gets
      designed against later.
      And **insistence needed no interrupt licence at all**:
      "told them, nothing changed" is a re-raise ledger — a heard `request`/
      `escalation` that no confirmed write follows gets ONE more mention at
      the next quiet full-gate tick, then is dropped. Twice is a peer; a
      third time is a nag. Still open from the original question: quiet
      hours, and a "not now" that persists.

### The slices

- [ ] **T1 — Spawning.** Blocked on nothing now: under Q2's handoff ruling this
      is the SAME write as T2 with different words in the body, so it adds no
      new authority. Do NOT let it become a tool that creates a session.
- [x] **T2 — Directing.** `msg send` to a session, gated by the confirm spine.
      Shipped in Slice 1 (§4a).
- [x] **T3 — Proactive interruption.** Shipped with Q3's settlement (#967):
      escalation-kind inbox messages ride the interrupt tier, everything else
      waits, and the re-raise ledger carries insistence without interrupting
      anything. **The producer half landed in #982** — the tier no longer fires
      only for what other sessions choose to escalate. Four fleet detectors now
      address the buddy directly through `agentwire/fleet_alerts.py` (generic
      typed-kind messaging; the ruling table and the throttle bounds live in
      [`sessions/messaging.md`](sessions/messaging.md#fleet-alerts--detectors-as-senders-982)).
      Two things about that wiring belong here rather than there:
      **(a) only two detectors got the interrupt tier** — expired login and a
      root pane blocked with no parent, both cases where nothing but a human
      clears it and something is burning meanwhile. A usage-limit park was
      demoted to `note` (it self-heals) and `worktree --dangling` was not wired
      at all (no autonomous trigger, nothing burning). The reasoning is Q3's,
      applied to the sending side: this tier's expensive failure is
      over-production, and a *true* alert the owner learns to ignore costs more
      than a missed one, because it retires the tier for every other producer.
      **And the latency bar is bigger than the 30s figure above**, which
      measures only the announcer legs: a detector's alert is ordinary inbox
      mail, so it first waits for the DRAIN, which rides the watchdog at
      `TICK_INTERVAL = 60`s. End to end an escalation from a detector is
      **~90s worst case**, not 30 — the 30s number is what applies to a message
      already in the spool. Nothing upstream of the drain is faster than a
      minute, so an event that is only urgent in its first seconds is not
      served by this channel at all, whatever kind it carries. **(b) The buddy's subscription is a lease, not a flag** — recorded in
      its identity record at `buddy register` and renewed at `buddy serve`.
      Because the buddy's mail is spooled rather than pasted, it never reads as
      "gone" to the drain, so a permanent subscription would keep filling a
      spool nobody reads and replay a fortnight of escalations at the next
      start — every one of them arriving as an interrupt. A bridge left running
      past its lease goes quiet until restart; that is the fail-safe direction
      and it is the residual to close if anyone ever runs one for days.
      Residual (still open): quiet hours, and a persistent "not now".
- [x] **T4 — Lifecycle host.** Shipped (#983). The registry grew a generic
      `command` service kind — a supervised process rather than an agent
      session — and §6 carries the paste-ready entry, the reasons the
      healthcheck is `tmux_session` rather than a poll of the token-bearing
      `/`, and the restart ruling with its pins. The one step deliberately NOT
      taken: the entry is not written into the owner's `config.yaml`. Residual,
      named rather than closed: a kill landing after the write dispatched but
      before the announcement is confirmed spoken leaves the write sent and
      unannounced.
- [x] **T5 — Confirm the confirm.** Satisfied by construction, not by a separate
      step: the approval surface IS speech (a spoken nonce), so there is no card
      to tap and nothing to reach for. DocumentScribe shipped a click-gated
      confirm modal a voice-only user could never reach (#748); this was never
      separable work from T2, and splitting it would have yielded an
      intermediate state strictly worse than not shipping — a write path with a
      confirm gate nobody can reach.
- [x] **Slice 1b — the `voice` kind (#985).** `voice` is in `inbox.KINDS` and
      `ESCALATE_KINDS`; attribution moved from the body front to the kind slot.
      The three hand-written `("done", "escalation")` tuples are gone, replaced
      by the one `inbox.load_bearing()` derivation, and the `_cohort_held`
      interaction (held by **sender**, harvested by **kind**) is pinned. See
      §4a.
      **The ruling this entry called for**, made by the owner on 2026-08-10
      rather than by the implementer, because it changes behaviour for non-voice
      sessions: `voice` is **ACTIVE** (a passive buddy write would sit
      undelivered until pulled, defeating the handoff — and would be a behaviour
      *reduction* versus the body prefix it replaces) and **IS in
      `ESCALATE_KINDS`** (the owner spoke it and walked away; screenless, a
      silent dead-letter is unrecoverable). It is **not** an interrupt —
      `ESCALATE_KINDS` governs dead-letter escalation, not the interrupt tier,
      and `escalation` remains the only kind that pre-empts. Full text with the
      kind table in
      [messaging.md](sessions/messaging.md#ruling-voice-is-active-and-escalatable-and-is-not-an-interrupt-985).

### Drift has two directions, and the second one is easy to miss

This page was reconciled against the code once and the reconciliation itself
created the reverse defect: the page became right and **six code sites stayed
wrong**, each still carrying the anchor wording #951 retired — "the
`response.done` of the turn in which the buddy SPOKE it" — in `client.py`'s
module docstring, `server.py`'s (twice), `confirm.py`'s (twice) and
`transcript.py`'s. Alongside them, the absolute #987 retired,
"`wait` denies unconditionally", still asserted in `confirm.py`; and
`voice_layer/__init__.py` still calling the surface read-only.

Two lessons, both cheap:

- **A claim that lives on two surfaces must be pinned on both.** Round one
  retired "`wait` denies unconditionally" on this page only, so the identical
  false sentence survived one file away, unpinned and unread. `confirm.py`
  records the same failure one level down: testing a table's entries against
  themselves proves the table, not the path into it.
- **A pin has to look where the defect lives.** The route pins were page-wide
  substring checks, and both routes appear in prose elsewhere — so deleting them
  from the architecture diagram, the exact thing those pins existed to catch,
  left every one of them green. They are anchored inside the fence now, and the
  route list is derived from `server.py`'s dispatch rather than typed.

### Known residuals — open, and named so nobody argues from a mechanism that isn't there

Each of these is a real hole in shipped code, filed rather than fixed, with the
reason it was not folded into the wave that found it. **A wiki that describes
what we meant is how the next contributor designs against something that does not
exist**, so these are listed here as well as inline.

| # | Where | The hole |
|---|---|---|
| #1009 | `confirm.py`'s `not_announced` note | its 30s/12s deferral bound is counted from the moment an item becomes `current`, and #997's `pump()` deferral sits UPSTREAM of that — so while a fallback utterance is live the stated bound under-states its own worst case by up to one speaking budget. A stated-guarantee defect, not a runtime one: the refusal is delivered either way |

**Closed, and named here because arguing from a residual that no longer exists
is the same failure in the other direction:** **#989** (the unheard window is
bounded on two axes — an arrived-but-empty transcript needs no clock, and a
missing one ages out on whether the audio buffer closed; see "Bounded await, and
outcomes that differ"), **#990** (`cancel()` takes the same claim, split on
`_dispatching` versus `_cancelled` so the losing cancel cannot assert an outcome
— see "(a) Proposal binding"), **#992** (the buddy's own voice cannot deny — its
own section below), **#995** (the four remaining `client.py` browser wires — the
data-channel `open`/`close` status wires and both button click handlers — now
pinned in `tests/unit/test_client_wires.py`, the `pc.ontrack` greet wire having
gone in #1001), **#996** (the `speakingBudget` watchdog now reports
`onNotSpoken`) and **#997** (`pump()` now defers to the browser voice, bounded
by that same budget). #996 and #997 are described in full under the watchdog
above.

What they have in common is where they were found: every one came out of a review
of a MERGED wave rather than out of the wave itself. Defects that survive
individual review live in the interaction between changes each correct alone.

And several carried a real trade rather than an obvious fix, which is why "just
close it" was not the whole instruction. The rulings live with the mechanisms
rather than here, but the trades are worth keeping together, because they are
one argument. #989's staleness bound: too tight and a slow transcriber becomes a
wrongful refusal, too loose and the loop survives — it took TWO bounds, split on
whether the audio buffer closed, because one flat age is wrong at one end.
#992's suppression rule: barge-in over the robot voice is the NORMAL case here,
so any rule of the form "utterances during fallback speech do not count as
denials" drops a genuine take-back and the write goes out — the acting-twice
direction, not a wait; the shipped rule is CONTENT, which cannot fire on words
the buddy never said and so has no such half. #997's deferral was the same shape
and is the worked example of pricing it: an unbounded "wait for the browser
voice" turns an audio-quality defect into a suppression defect, which in a
screenless channel is strictly worse than the thing being fixed, so the wait is
bounded by the budget that decides when the page stops believing the voice at
all. Price both halves before choosing.

(#995 and #996 had no such tension. #990 looked like it had none either, and
that reading was wrong twice: the first fix told a cancel racing the AWAIT that
the write was already going out, and the second still let a cancel arriving
between `_succeeded.add` and the claim's release say nothing had been sent. A
defect whose diagnosis is obvious can still have a fix that is not.)

### Standing constraints for whoever picks this up

- **It ships to `main` behind `beta.voice_layer`, default OFF** (owner ruling
  2026-08-10, reversing the earlier never-merge constraint). It still runs on
  the user's own API key. Anything you add that would reach a user who has not
  opted in — a shipped role-prompt line, a registry entry, a portal route —
  goes inside the gate, and the byte-identity bar in §0 is what says whether
  you got it right.
- **The boundary in §2 is not negotiable.** The buddy starts and directs Claude
  sessions. It never does the work itself. Anything that reads as "it could just
  fix that typo itself" reintroduces what #730 removed.
- **Verify model ids with `GET /v1/models/<id>`**, never by minting a client
  secret — see §1.

---

## Appendix — the #689 heal line-count cliff (#930)

*Filed here, not under the confirm spine, because it is a property of
`flush_session` and the #689 heal that every long or coalesced message in the
system is subject to. It was measured while building the voice layer; it is
not about the voice layer.*

**This is not a voice-layer fact and it should not be filed as one.** It is a
property of `flush_session`'s `stuck` test and the #689 heal, and every long or
coalesced message in the system is subject to it. Written up here because this
is where it was measured; the fix belongs to #930.

Reproduce with `tools/voice_heal_probe.py`, which creates its own throwaway
80x24 tmux session running Claude Code, pastes real rendered messages, leaves
the Enter unsent, and runs the actual heal. **A number without the method is a
number the next person will distrust and re-derive**, so the method ships.

### The wrong model, and who held it

The intuitive model is *chip versus text*: a paste either renders as
`[Pasted text #N +M lines]` (heal fails) or as text (heal works). **That model
is wrong, and it survived two independent sessions whose job was to be
skeptical** — it is what the spec author and the adversarial reviewer both
worked from, and the reviewer asserted it in a written finding. Its own summary
afterwards: *"listing a failure mode without ranking or measuring it is not much
better than missing it; I gave you a boundary claim dressed as measured when
only the direction was."*

There are **two failure regimes**, and the box windows long before it chips.

### Regime 1 — a single long line WINDOWS (measured, 80x24)

| Rendered line | Box holds | `stuck` test |
|---|---|---|
| 430 | 440 | hit ✓ |
| 520 | 532 | hit ✓ — last passing |
| 540 | 480 | **miss** — the box renders only a window |
| 880 | 16 | **miss** — now a chip |

So `stuck` fails from ~530 chars, a full ~350 chars *before* the chip appears.
"It isn't a chip" is not evidence the heal will fire.

### Regime 2 — FOUR OR MORE LINES chip, at any size (measured)

Line count, not character count, is the trigger:

| Lines | Chars | Box holds | Chip |
|---|---|---|---|
| 2 | 43 | 45 | no |
| 3 | 65 | 69 | no |
| **4** | **87** | **25** | **yes** |
| 6 | 131 | 25 | yes |

The same 87 characters on **one** line renders as text (box 89, no chip). Four
lines chips at 87 characters.

### Why that matters: the drain coalesces

`flush_session` joins the whole queue into ONE paste with a **newline**
(`inbox.py:1059`, `"\n".join(m.render() for m in messages)`) and then tests
**each** message's render against that single box content. Measured with real
messages:

| Queued | Chars | Box | `stuck` hits |
|---|---|---|---|
| 1 | 128 | 130 | 1/1 |
| 2 | 257 | 263 | 2/2 |
| 3 | 386 | 396 | 3/3 |
| **4** | **515** | **25 (chip)** | **0/4** |

**A drain coalescing four or more messages wedges every one of them** — no
matter how short each is, and with every per-message cap fully respected.

**Two regimes, two DIFFERENT governing variables — and this is the part a fix
can get wrong.** It is tempting to summarise all of the above as "line count,
not characters". That is right about the *chip* and it understates the picture:

| Regime | Governed by | Chip? | `stuck` |
|---|---|---|---|
| Windowing | **characters** (~530+ on one line) | **no** | miss |
| Chip | **lines** (4+, at any size) | yes | miss |

The decisive pair, from the measurements above: **530 characters on ONE line
does not chip** (box 467 — it windows), while **515 characters on FOUR lines
does** (box 25). Fewer characters, more lines, chip appears. Both wedge
identically.

**Consequence for #930: a fix that addresses only the 4-line chip cliff leaves
the character-governed windowing wedge open.** Any probe carried by that work
needs rows for both.

*Prediction, explicitly NOT a measurement:* from these numbers, a 3-line blob
over roughly 470 characters should window without ever chipping. Worth a row in
#930's probe; label it a prediction until someone measures it.

Two further consequences worth stating plainly:

- **The variable that governs is the COALESCED length and line count, not the
  message length.** No cap expressed per-message can bound either regime. A message that
  merely happens to be queued behind three others crosses the cliff through no
  fault of its own.
- **The coalesced blob is multi-line by construction**, because the join is a
  newline. So every multi-message drain already has the property single messages
  are careful to avoid, and has since coalescing landed. Combined with a
  swallowed Enter — the condition the #689 heal exists for — the result is a
  **permanent wedge: never healed, never dead-lettered, therefore never
  emailed**, surfacing only via `doctor` after two hours.

  **Do not read that as rare.** The first version of this note called it rare
  because it needs "two intermittent conditions at once" — that was wrong, and
  wrong in the same way an over-claim is wrong, just pointed the other way.
  Four-plus coalesced messages is **routine on a busy fleet**, not intermittent:
  it is the ordinary state of a recipient that has been busy for a minute. So
  only one condition is actually intermittent, and **the rate is governed by the
  swallowed-Enter path alone**. What makes it unreported is that it is silent,
  not that it is uncommon.

### Caveats on the numbers

Measured at **80x24**. The box shows a bounded number of ROWS, so **a shorter
pane windows sooner**. Treat these as an upper bound for that geometry, not as
constants.

### Why a live probe rather than a fixture

The probe **failed on its first run** for a reason a fixture structurally cannot
produce: it read the box too early and measured a partially-rendered paste — 38
chars for a 159-char body. A fixture is fully rendered by construction, so it
can never show you that. That is the argument for the probe existing.

