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
- **One write: a message to a session that is already running** (§4a below).
- Speaks only when spoken to.

**Does not — and this is where the risk lives:**
- ❌ No spawning, no session creation, no worktrees. Ever. See §4b.
- ❌ No acting directly on the fleet — every write is a request to a session.
- ❌ No proactive interruption.

There is deliberately **no escape hatch**. Adding a capability means adding a
tool, in a diff someone reviews.

## 4a. The confirm spine

The buddy's one write is gated below the model, in two halves that do different
jobs.

### The guarantee, in full

> This defends against **mis-transcription and against an approval the
> conversational model invented**, which is the stated threat. It does **not**
> cover every mis-transcription — a transcriber hallucination or an
> approval-shaped utterance meant for someone else is a real residual risk that
> the nonce narrows but does not eliminate. **A passed gate means the message
> was queued, not delivered, and not acted on.** It is **not** a security
> boundary against an adversary.

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
rate-limited instead.

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
false-reject half produces a gate nobody can pass. Hence one-spelling words,
normalization on both sides, and **containment rather than whole-utterance
matching** — strictness was inherited from a grammar ("yes") that carried no
entropy, and the nonce carries its own.

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
- a proposal is anchored at the **`response.done` of the turn in which the buddy
  spoke it**, which is when the owner heard what they would be approving.

The transcript forward is awaited before any function call dispatches — they are
independent `fetch` calls otherwise — and the ring holds a lock, because the
bridge is a `ThreadingHTTPServer` and a confirm blocks on the ring's condition
waiting for the transcript it needs.

### Bounded await, and outcomes that differ

Fail-closed *immediately* is wrong: the conversational model starts generating as
soon as VAD commits while transcription is a separate pass, so a confirm often
beats its own transcript. Refusing instantly would tax every confirm two
utterances — and leave the first approval stale in the ring, so a retry after
"no, wait" would **write after the owner said no**. So the gate waits ~2.5s on a
condition variable, a matched utterance is *spent*, and any denial committed
after an approval refuses.

Outcomes are keyed on **what the owner should do next**, and are never
collapsed:

| Outcome | Owner's correct next move |
|---|---|
| `no_proposal` | restate the request |
| `expired` | ask again |
| `not_announced` | wait — the buddy hasn't finished saying it |
| `replayed` | nothing, it already went |
| `refused` | say the phrase |
| `wrong_nonce` | ask what the word was |
| `denied` | nothing — you said no |
| `pending_transcript` | **wait** |

`refused` and `pending_transcript` demand *opposite* behaviour. Collapsing them
trains the owner to repeat into a system that needed them to hold still.

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
response (an error from cancelling an already-finished one is ignored), issue a
scripted `response.create`, and verify against the following `response.done`.

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

Slice 1 ships the body half:

```
[MSG from buddy · request] <voice> restart the portal ┃ said: "confirm tango" ┃ #a1b2c3
```

The `<voice>` marker goes **first in the body** and that placement is the whole
of Slice 1's attribution. With `--kind request` the kind slot distinguishes
nothing, so the only prefix-level distinguisher left would be exactly the sender
string §4 rejects; putting the marker at the front of the body puts it in the
position the kind slot would have occupied, and touches no shared code. **Slice 1
does not claim kind-slot attribution** — that arrives with the `voice` kind in
Slice 1b.

The verbatim authorizing utterance rides along free, because the gate already had
to capture it. A recipient can always answer "did a human really say this, and in
what words", and can see it when the buddy mis-paraphrased.

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

### The #689 heal has a line-count cliff — a MESSAGING-subsystem fact (#930)

**This is not a voice-layer fact and it should not be filed as one.** It is a
property of `flush_session`'s `stuck` test and the #689 heal, and every long or
coalesced message in the system is subject to it. Written up here because this
is where it was measured; the fix belongs to #930.

Reproduce with `tools/voice_heal_probe.py`, which creates its own throwaway
80x24 tmux session running Claude Code, pastes real rendered messages, leaves
the Enter unsent, and runs the actual heal. **A number without the method is a
number the next person will distrust and re-derive**, so the method ships.

#### The wrong model, and who held it

The intuitive model is *chip versus text*: a paste either renders as
`[Pasted text #N +M lines]` (heal fails) or as text (heal works). **That model
is wrong, and it survived two independent sessions whose job was to be
skeptical** — it is what the spec author and the adversarial reviewer both
worked from, and the reviewer asserted it in a written finding. Its own summary
afterwards: *"listing a failure mode without ranking or measuring it is not much
better than missing it; I gave you a boundary claim dressed as measured when
only the direction was."*

There are **two failure regimes**, and the box windows long before it chips.

#### Regime 1 — a single long line WINDOWS (measured, 80x24)

| Rendered line | Box holds | `stuck` test |
|---|---|---|
| 430 | 440 | hit ✓ |
| 520 | 532 | hit ✓ — last passing |
| 540 | 480 | **miss** — the box renders only a window |
| 880 | 16 | **miss** — now a chip |

So `stuck` fails from ~530 chars, a full ~350 chars *before* the chip appears.
"It isn't a chip" is not evidence the heal will fire.

#### Regime 2 — FOUR OR MORE LINES chip, at any size (measured)

Line count, not character count, is the trigger:

| Lines | Chars | Box holds | Chip |
|---|---|---|---|
| 2 | 43 | 45 | no |
| 3 | 65 | 69 | no |
| **4** | **87** | **25** | **yes** |
| 6 | 131 | 25 | yes |

The same 87 characters on **one** line renders as text (box 89, no chip). Four
lines chips at 87 characters.

#### Why that matters: the drain coalesces

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

Two consequences worth stating plainly:

- **The variable that governs is the COALESCED length and line count, not the
  message length.** No cap expressed per-message can bound it. A message that
  merely happens to be queued behind three others crosses the cliff through no
  fault of its own.
- **The coalesced blob is multi-line by construction**, because the join is a
  newline. So every multi-message drain already has the property single messages
  are careful to avoid, and has since coalescing landed. Combined with a
  swallowed Enter — the condition the #689 heal exists for — the result is a
  **permanent wedge: never healed, never dead-lettered, therefore never
  emailed**, surfacing only via `doctor` after two hours. Two intermittent
  conditions, so it would be rare and completely silent, which is consistent
  with nobody having reported it.

#### Caveats on the numbers

Measured at **80x24**. The box shows a bounded number of ROWS, so **a shorter
pane windows sooner**. Treat these as an upper bound for that geometry, not as
constants.

#### Why a live probe rather than a fixture

The probe **failed on its first run** for a reason a fixture structurally cannot
produce: it read the box too early and measured a partially-rendered paste — 38
chars for a 159-char body. A fixture is fully rendered by construction, so it
can never show you that. That is the argument for the probe existing.

### What the voice layer's cap does and does not buy

`MAX_BODY_CHARS = 300` and `MEASURED_STUCK_LIMIT_CHARS = 520` live in the voice
layer as a **caller-side** cap. Making the cap a property of `msg` itself is the
right long-term shape (#930) and is deliberately not done here: it changes
behaviour for every sender in a shared subsystem, which is the same class of
change as the `voice` kind deferred to Slice 1b, and it deserves its own review.

**The residual, stated rather than implied.** The one-line rule protects the
**single-message case**: a lone voice write is well inside both regimes (max
rendered body 279, max rendered line 317, against a measured 520 and a 4-line
cliff). It does **not** protect a voice write that is coalesced behind other
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
(maxed body + the longest worktree sender name) near 385 against a measured
520.

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

### Why `--kind request` and not a `voice` kind

A `voice` kind is deferred to Slice 1b deliberately. It is the only part of this
work that would change behaviour for sessions with nothing to do with voice, it
touches a shared subsystem's escalation and dead-letter paths, and its blast
radius is non-obvious: `doctor_cli.py:1182` and `session_cli.py:1280`/`:1321`
filter a hand-written `("done", "escalation")` tuple, so a dead-lettered message
of a new kind would be invisible to `doctor`'s `[!!]` line and to `session
info` — silently defeating the argument the kind was being made for.

`request` is already in `ESCALATE_KINDS`, so the dead-letter-emails-the-owner
property is achieved today. When 1b lands it should fix those three call sites
by **deriving from `inbox.ESCALATE_KINDS`** rather than adding a fourth
hand-written tuple.

**Two things the substitution drags in**, both named rather than discovered
later:

- **That `doctor` / `session info` gap already bites `request` today**, entirely
  independent of voice — a dead-lettered `request` is invisible to both right
  now. Worth fixing on its own merits; filed separately, not folded in here.
- **`request` IS in `cohort.REPORT_KINDS`**, and `cohort._harvest` filters on
  kind — so if the buddy were ever enrolled as a pending cohort child of the
  recipient, `wait --children` would harvest the buddy's write as a child report
  and consume it. What makes that unreachable today is that nothing enrols the
  buddy; both halves are asserted in
  `tests/unit/test_voice_body_delivery.py::TestTheCohortInteraction`, so if
  enrolment ever changes the test fails rather than the report vanishing.

## 4b. Cold fleet: the buddy never starts an orchestrator

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
      orchestrator (§4b).
- [ ] **Q3 — What earns the right to interrupt?** Needed before any proactive
      speech. Candidate triggers, roughly in descending defensibility: a
      dead-lettered `done`/`escalation` (something is already lost), a dangling
      PR with no live parent (#716), a session parked on a usage limit, a
      scheduled task that failed. Everything else is noise. Also needs a
      quiet-hours answer and a "not now" that persists.

### The slices

- [ ] **T1 — Spawning.** Blocked on nothing now: under Q2's handoff ruling this
      is the SAME write as T2 with different words in the body, so it adds no
      new authority. Do NOT let it become a tool that creates a session.
- [x] **T2 — Directing.** `msg send` to a session, gated by the confirm spine.
      Shipped in Slice 1 (§4a).
- [ ] **T3 — Proactive interruption.** Technically easy — the spool is already
      there — and socially the hardest to get right. An assistant that
      interrupts badly gets turned off. Blocked on Q3.
- [ ] **T4 — Lifecycle host.** Wire §6's `services.custom` entry, if and when
      the owner wants the buddy on the startup path. Independent of Q1–Q3; safe
      to do first.
- [x] **T5 — Confirm the confirm.** Satisfied by construction, not by a separate
      step: the approval surface IS speech (a spoken nonce), so there is no card
      to tap and nothing to reach for. DocumentScribe shipped a click-gated
      confirm modal a voice-only user could never reach (#748); this was never
      separable work from T2, and splitting it would have yielded an
      intermediate state strictly worse than not shipping — a write path with a
      confirm gate nobody can reach.
- [ ] **Slice 1b — the `voice` kind.** Add `voice` to `inbox.KINDS` and
      `ESCALATE_KINDS`, moving attribution from the body front to the kind slot.
      Deliberately split out of Slice 1: it is the only part that changes
      behaviour for sessions with nothing to do with voice. Must ALSO fix
      `doctor_cli.py:1182` and `session_cli.py:1280`/`:1321` by deriving from
      `inbox.ESCALATE_KINDS` rather than adding a fourth hand-written tuple, and
      must test the `_cohort_held` interaction (it filters by **sender**, not
      kind). See §4a.

### Standing constraints for whoever picks this up

- **This branch is not for merge.** Personal project, owner's own install,
  owner's own API key. Draft PR only; never mark ready, never merge to `main`.
- **The boundary in §2 is not negotiable.** The buddy starts and directs Claude
  sessions. It never does the work itself. Anything that reads as "it could just
  fix that typo itself" reintroduces what #730 removed.
- **Verify model ids with `GET /v1/models/<id>`**, never by minting a client
  secret — see §1.
