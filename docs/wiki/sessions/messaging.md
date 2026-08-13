# Polite agent-to-agent messaging (`agentwire msg`)

> A non-interrupting channel for sessions to talk amongst themselves — it never
> clobbers a human who is mid-typing.

## The problem it solves

The only channel into a running session used to be `agentwire send` /
`session_send`: it pastes text into the prompt and presses Enter **right now**.
There is no check for whether the input box already holds uncommitted text. So
when a worker reports back while you're half-way through typing a long message,
the worker's text is appended to your draft and the whole thing is submitted
together. Garbage in, garbage out.

`agentwire send` stays exactly as it was — forceful, immediate control is a
feature when you actually want it. `msg` is its **polite sibling**: the message
lands in a durable inbox and is injected only at a safe boundary.

| | `agentwire send` / `session_send` | `agentwire msg` / `msg_send` |
|---|---|---|
| Delivery | Immediate paste + Enter | Queued; injected when safe |
| Collision with a human draft | **Clobbers it** | **Never** — waits for the box to clear |
| Latency | Instant | ≤60s (rides the watchdog) |
| Use when | You must drive a session *now* | Routine peer updates that shouldn't interrupt |

## How it works

1. **Enqueue.** `msg send` writes one JSON file per message into the recipient's
   inbox dir, `~/.agentwire/inbox/<session>/<epoch_ns>-<uuid>.json`, atomically
   (`*.tmp` then rename). Filename order = delivery order. "ls is the protocol"
   — same pattern as [Council](../council.md)'s file inbox.

2. **Drain.** A flush loop rides the existing [usage-limit watchdog](../usage-limit-recovery.md)
   tick (`agentwire limits tick`, every 60s), after the usage-limit and
   [prompt-routing](prompt-routing.md) sweeps. For each inbox it delivers
   **only when both gates pass**:
   - `prompt_is_empty(session)` — the input box holds no uncommitted text.
   - `safe_deliver` guards — the session isn't parked, the pane runs an agent
     (not a shell/editor), and no live menu/dialog is on screen.

3. **Inject.** When the box is clear, queued messages are coalesced into a
   single paste (one submit) and delivered via the verified-delivery path
   (`session_ready.send_verified`), each rendered as
   `[MSG from <sender> · <kind>] <text>  ⟨#<id6>⟩`. The trailing `⟨#id6⟩` token
   (the message's short uuid) makes every delivered line **unique on screen**, so
   the idempotent-redelivery dedup below can full-line match without a shorter
   message substring-colliding against a longer same-sender/kind one.

   **Idempotent delivery (the load-bearing #621 guard).** `send_verified`
   confirms submission by polling the input box back to empty; under host load
   that confirm can **false-negative even though the paste landed** and the
   recipient saw it. Retaining a landed message re-injects it on every idle tick
   — forever (the field repro: ~11 report-backs replayed all session). So the
   drain treats delivery as **idempotent**: before and after a paste it checks
   the recipient's 200-line scrollback **per message** (each message's own
   full rendered line, via `session_ready.message_on_scrollback` — a strict
   match that ignores the generic `[Pasted text]` placeholder), and any message
   already visible is consumed (unlinked) instead of re-pasted. A
   `delivery_unverified` for a paste that genuinely vanished still penalizes
   normally. The same hardening lives one layer down: `send_verified`'s Phase-2
   confirm now keys on *"the box no longer holds our text"* (Phase 1 already
   proved it landed) rather than demanding a spinner / echoed turn — so a quiet
   or fast agent no longer makes a landed-and-submitted paste look unverified.
   That one fix covers the polite-msg loop, `notify-parent` (which also routes
   through `safe_deliver` → `send_verified`), and `session_send`.

   Two further #667 hardenings live in the same layer, for every
   `send_verified` caller: **(a) full-message identity** — the land/confirm
   checks key on the full whitespace-normalized message, never a fixed-length
   prefix (all worktree idle notifications share a >32-char
   `[NOTIFY from agentwire-dev-issue-…` prefix, so a fragment false-matched a
   *pile* of other sessions' notifications sitting in the box); and **(b) no
   blind re-paste** — before pasting, each attempt checks whether the message
   already sits landed-but-unsubmitted in the box, and if so retries only the
   *submit*, so a whole-send retry can never double the draft. "Already in the
   box" is **window-aware** (#851): the input box has a bounded visible height
   and scrolls, so a draft taller than it renders only a contiguous window of
   itself — accepted as ours when it is at least `MIN_BOX_FRAGMENT` (80
   normalized chars) long and shorter than the message, which is what keeps a
   short foreign draft ("ok") from reading as our paste landing. When Escape
   can't clear such a draft, `clear_input_box` escalates to a bounded backspace
   sweep.

   **Nothing pastes onto a foreign draft (#845).** The identity guard above
   proves only that the box does *not* hold our message; it said nothing about
   what it *does* hold, so a stale, different draft — a previous sender's
   swallowed message, a human mid-typing, a half-composed large paste — got our
   text pasted on top of it, and one Enter submitted the concatenation as a
   single garbled turn. `session_ready.box_holds_foreign_draft` names that
   state (plain box parse says non-empty and not-ours, then the SGR-aware
   `prompt_is_empty` vetoes, so dim ghost/autosuggest text never counts), and
   `_deliver_once` **refuses** rather than pasting — it is the delivery
   primitive and cannot tell a human's sentence from wedged wreckage. Box
   surgery stays in the recovery layer: `agentwire send --verify/--wait-ready`
   look at the box *before* attempting anything, and a draft that predates the
   attempt is queued to the msg inbox with `recover_failed_seed(clear=False)`
   → outcome **`inbox_blocked`** (queued, box left untouched) rather than the
   `inbox`/`inbox_stuck` clear-then-queue path (#843/#844).

   **Per-attempt delivery markers (#839).** A direct verified send tags its
   paste with `session_ready.new_delivery_marker()` — a unique
   `⟨#send-xxxxxx⟩` token appended via `tag_message`, mirroring the drain's own
   `⟨#id6⟩` tail. The token rides inside the pasted text, so the landing gate,
   the idempotent-paste guard and the unverified-send fallback all key on
   something unique to *this* attempt. Without it, `_recover_unverified_send`
   matched the bare prompt against scrollback, and a short or generic message
   ("yes", "continue", "approved") that happened to sit in the last 200 lines
   for an unrelated reason reported `already_delivered` and **skipped the inbox
   enqueue entirely** — silently dropping a send that never landed, the one
   outcome the fallback exists to prevent.

   **Pasted ≠ submitted (#689, hardened by #698).** Closures for the
   paste-lands-but-Enter-is-swallowed failure: **(a)** `message_on_scrollback`
   excludes the input-box region — only lines strictly *above* the box's top
   border count, so a message still sitting in the box (even on a mid-redraw
   frame missing the bottom border) never reads as "on scrollback" and the
   drain can't unlink a pending file the recipient never received (an
   unparseable box counts as *not* on scrollback: keep pending). **(b)**
   `send_verified` only ever trusts a **parseable box** (#698): Phase 1 ends
   only when the text renders in the box or the message/marker is echoed
   *outside* it (empty box + ambient activity glyphs is what a pane looks like
   in the instants before a paste renders — the old check confirmed there with
   zero Enter presses), and the Phase-2 confirm is "a parseable box no longer
   holds our text" with garbled/unparseable frames never confirming (activity
   glyphs and raw-capture matches are satisfied by a stuck paste inside the
   very box that failed to parse). A live select-menu appearing mid-send
   aborts both the paste and the Enter loop. **(c)** When the drain finds one
   of its own pending messages rendered in the recipient's box, it heals via
   `session_ready.finish_submit` — an **Enter-only** retry (never a re-paste,
   so the #621 dedup holds), unlinking only once submission confirms and
   otherwise deferring without penalty (`stuck_in_box`). **That "finds one of
   its own" test has a measured cliff** — the box windows or chips long before
   you'd expect, so a coalesced drain of four or more messages wedges every one
   of them with no dead-letter and no email:
   [The #689 heal cliff](heal-line-count-cliff.md) (#930). As a last-resort
   backstop, the watchdog pane-sweep flushes a bare Enter on any pane —
   including a mid-generation one, where Enter merely queues the draft (#698;
   the old spinner gate left a stuck box on a busy orchestrator unrescued) —
   whose box has held identical **machine-injected** text (`[MSG…`, `[NOTIFY…`,
   `[Pasted text…`) for two consecutive sweeps; unjudgeable frames (garbled
   box, live dialog) hold the counter rather than resetting it, and
   human-looking drafts are never auto-submitted. Pastes themselves go out
   with bracketed-paste delimiters (`paste-buffer -p`), so a trailing Enter
   can't be coalesced into the paste burst as a newline.

4. **Defer or drop.** If either gate fails, the messages stay put, their
   `attempts` counter bumps, and the defer `reason` (`box_not_empty`,
   `target_not_agent`, …) is stamped on each message. After `MAX_ATTEMPTS`
   (40 ≈ 40 min of a permanently busy session) a message moves to
   `~/.agentwire/inbox/<session>/dead/` carrying that reason + a `dead_ts`, and a
   `dead_letter` event is logged — no infinite retry. `msg dead` surfaces these
   so the drop is never silent.

   Three refinements keep the penalty honest:

   - **No-penalty "can't take it right now" reasons.** `target_busy` (the box
     can't be parsed — the agent is running a long command) and
     `queued_placeholder` (the box shows Claude Code's *"Press up to edit queued
     messages"* — the agent is generating with human-queued input) are *busy*,
     not refusals. They defer **without** bumping `attempts`, so a legitimately-busy
     session never burns a report-back toward dead-letter; the message waits and
     delivers once the box frees up. The placeholder is matched loosely, and only
     the *penalty* changes — a non-empty box is still never pasted into (see the
     collision detector below). `box_static` and `stuck_in_box` join them for the
     same reason.

     **`target_parked` too (#872).** A usage-limit parked recipient exists and
     will come back — recovery parses the reset time and nudges the session
     afterward — it just can't be pasted into without corrupting the resume. It
     was penalized, which capped tolerance at `MAX_ATTEMPTS` ticks ≈ 40 minutes
     against a park that routinely runs hours; every `done` a parked parent's
     workers filed died before the reset landed. It is the most clearly temporary
     member of the set, and now defers without penalty like the rest.

     **Penalty-free means invisible unless something looks (#879).** Never
     dead-lettering also means never firing the dead-letter owner email, which is
     the only *unprompted* signal in the whole path. That was harmless while every
     no-penalty reason was a short-lived box state; admitting `target_parked`
     changed it, since a park can legitimately last hours. So `agentwire doctor`
     reports load-bearing (`ESCALATE_KINDS`) messages still
     pending past `inbox.STALE_PENDING_MS` (2h), naming the recipient, the wait,
     and the defer reason — and flagging parked recipients as self-resolving so
     the section reads as FYI rather than failure. `agentwire msg inbox -s
     <session>` remains the on-request view. Deliberately *not* an owner email: a
     multi-hour park is the expected shape now, so emailing on it is the noise
     that gets the channel muted.
   - **Gone recipients burn out fast (#694).** Before any box parsing, the
     drain checks the target against the live tmux session list. A recipient
     that *positively doesn't exist* defers as `target_gone` — a penalized
     reason with its own short cap, `GONE_MAX_ATTEMPTS` (5 ticks ≈ **5
     minutes**, counted in a separate per-message `gone_attempts` field so
     busy penalties accrued while the target lived don't erode the grace). A
     gone session can't un-go by itself — the grace only needs to cover a
     recreate/restart landing — so it doesn't get the 40-minute busy window.
     The up-front check also fixes the original hole: capturing a gone
     session parses as "no box" → `target_busy`, a no-penalty defer, which is
     how a `done` to a stale parent once sat queued for ~24 hours instead of
     escalating. Only *positive* knowledge counts: if tmux itself is
     unreachable (server down, e.g. mid-reboot), that's an outage rather than
     a gone recipient, and the ordinary no-penalty defer path applies.
     Messages to remote (`name@machine`) recipients — which the local drain
     could never deliver anyway (see Scope) — now surface as `target_gone`
     within minutes instead of pending silently forever.
   - **Out-of-band escalation.** When a **load-bearing** kind (`done` /
     `request` / `escalation` / `voice`) does dead-letter, the owner is emailed
     via the shared Resend
     wiring (the same channel usage-limit parking uses) so the loss is surfaced
     even if nobody runs `msg dead`. `note` is fire-and-forget and `ingest` never
     auto-delivers, so neither is escalated. Escalation is **batched per drain
     pass, not per message** (#829/#830): every message dead-lettered in the
     same batch for one recipient gets a single digest email (detail capped at
     the first 10, "...and N more" beyond that) instead of one email each — a
     recipient stuck permanently undeliverable (e.g. wrongly parented to a
     service session that never drains) can't spam the owner one email per
     stuck message. Escalation is best-effort — a send failure is logged
     (`dead_letter_escalate_failed`) and never breaks the drain.

### `prompt_is_empty` — the collision detector

The one genuinely new building block (`prompt_router.prompt_is_empty`). It reads
the bottom of the target pane with `capture-pane`, finds the Claude Code input
box (the region between the last two `─` rule lines), strips the `❯` glyph, and
returns `True` only if what remains is empty.

It is **conservative by design**: any non-empty content (a human draft *or* a
busy-state placeholder like "Press up to edit queued messages") and any screen
it can't parse as a clean empty box return `False`. A delayed message is fine; a
clobbered draft is not.

The queued-message placeholder is non-empty here too, so `prompt_is_empty` stays
`False` and the box is never pasted into — the distinction between a *draft* and
the *placeholder* lives one layer up, in the drain's penalty decision
(`prompt_router.is_queued_placeholder`), not in this guard. That keeps the
collision detector simple and the no-clobber guarantee absolute.

## Typed messages

`--kind` is a small enum (Overstory-inspired), not a workflow engine:

| kind | meaning |
|---|---|
| `note` | default — informational |
| `done` | a worker finished — also what idle report-backs ride: `agentwire notify-parent --queued` (used by `idle-handler.sh`, #667) enqueues here instead of direct-pasting |
| `request` | asking for something |
| `escalation` | needs attention — **the only kind a consumer may act on out of turn** |
| `ingest` | **passive** — awareness only; never auto-delivered (see below) |
| `voice` | the owner speaking through their [voice buddy](../voice-layer.md) — active and escalatable, but not an interrupt (see the ruling below) |

An optional `--ref` carries a machine-readable pointer (e.g. a report path)
alongside the text, surfaced as a typed field rather than parsed out of prose —
ideal with `ingest`.

Two attributes cut across the enum, and they are **different axes** that are
easy to collapse into one:

- **`PASSIVE_KINDS` = (`ingest`,)** — never auto-delivered, so it cannot drive
  the recipient into a turn. Everything else is *active*.
- **`ESCALATE_KINDS` = (`done`, `request`, `escalation`, `voice`)** —
  load-bearing, so a dead-letter emails the owner, `doctor` reports it, and
  `worktree --list` badges it. `note` is fire-and-forget; `ingest` is pull-only
  by design.

Neither is the **interrupt tier**. `escalation` alone pre-empts, and it is a
one-member set that no other kind joins.

### Ruling: `voice` is active and escalatable, and is not an interrupt (#985)

Before #985 the buddy marked a message as voice-originated by prefixing the
message **body** with a `<voice>` tag, riding `--kind request`. Attribution sat
inside the text while the slot that actually drives behaviour said something
else. `voice` moves it into the slot. **Owner ruling, 2026-08-10:**

- **ACTIVE.** A `voice` message *is* the owner talking to a session through the
  buddy, so it should drive that session exactly as typing at it would.
  Delivered when the input box is empty, like `note`/`request`. Making it
  passive would have been a behaviour *reduction* versus the body prefix it
  replaces — this is a consistency/SSOT change and must not quietly remove a
  capability.
- **IN `ESCALATE_KINDS`.** The owner spoke it and walked away. Screenless, a
  silently dead-lettered voice message is unrecoverable: there is no screen on
  which to notice the graveyard entry.
- **NOT an interrupt.** `ESCALATE_KINDS` governs *dead-letter escalation*; the
  interrupt tier is a separate axis. `escalation` remains the only kind that
  pre-empts — see `inbox._alert_dead_letters`' promotion (keyed on `escalation`
  alone) and the buddy client's `isUrgent`. Widening either to `voice` would
  make every routine spoken message an alarm, which is exactly the "retires the
  tier" failure the fleet-alert ruling below exists to avoid.

One consequence worth stating, because the two halves filter on different
fields: `inbox._cohort_held` holds by **sender**, while `cohort.REPORT_KINDS`
harvests by **kind** and deliberately excludes `voice` (the owner is not a child
reporting on a task). A `voice` message from a session that *is* a pending
cohort child is therefore held but not harvested — a deferral, not a loss: it
stays pending and delivers once the cohort resolves, the same shape `ingest`
already has.

**One derivation, not four literals.** `ESCALATE_KINDS` is read through
`inbox.load_bearing()`. Before #985, `doctor`'s dead-letter section and both
`worktree --list` / `--watch` each hand-wrote `("done", "escalation")` — already
disagreeing with `ESCALATE_KINDS` about `request`, and one merge away from
disagreeing about `voice` on the one kind whose sender is screenless. Add a
consumer by calling `load_bearing()`; never by retyping the tuple.

## Passive `ingest` — awareness without being driven

Every other kind is *driving*: the watchdog pastes it (and presses Enter) into
the recipient's prompt the moment their box is empty — which **starts a turn**.
`ingest` is the exception. It routes to a reserved `ingest/` subdir that the
drain and watchdog never walk, so it lands **silently** and waits. The recipient
collects it on their own cadence with `msg pull` (MCP `msg_pull`) — read **and**
remove. Nothing about an `ingest` message ever drives the recipient.

This is the primitive behind **[Briefing Mode](../briefing-mode.md)**: a
correspondent drops `msg send --kind ingest --ref <report-path> "<topic>"`; the
anchor stays quiet until the human says "what's ready?", then `msg pull`s the
pointers and reads the files. The durable content lives in the referenced file,
not the message — so `pull` (consume-on-read) is the only way these leave the
inbox; they are never dead-lettered.

## Fleet alerts — detectors as senders (#982)

Until #982 every kind above had exactly one class of sender: an agent typing
`msg send`. The machine's own detectors — expired login, usage-limit park,
dead-lettered report-backs, a root session blocked with nowhere to route — could
only reach the **owner**, by email. `agentwire/fleet_alerts.py` is the other
half: the same detectors, addressed as typed mail to any session that asks for
it. Nothing about the email path changed; this rides alongside it.

**Subscription is a lease, not a flag.** A session opts in with
`fleet_alerts.subscribe(name)`, which records an expiry in that session's
`metadata.json` (the #871 store — no second registry to drift) and must be
renewed. That is not ceremony: the drain's liveness gates are about *pasting
into a pane*, so a recipient that collects its mail some other way never reads
as "gone" the way a dead tmux session does. A permanent flag would keep
producing into a queue nobody is draining and then hand over the whole backlog
at once whenever that recipient came back — every message still carrying the
priority it was sent with, long after any of it was actionable. An expired lease
fails **quiet**, the correct direction for a producer whose expensive failure is
over-production.
With no subscriber, `emit` walks the store, finds nothing, and returns — every
detector behaves exactly as it did before.

### The ruling: which detector earns which kind

`escalation` is the only kind a consumer may act on out of turn — every other
kind waits for the recipient to be free. So the bar is **not** "is this
event real?" — all five candidates are real when they fire. It is: *can this
clear without a human, and is something burning while it waits?*

| Detector | Kind | Why, and what bounds it |
|---|---|---|
| expired login (`auth_expired`) | `escalation` | Machine-wide; every subsequent turn is refused and only `/login` clears it. Once per `ESCALATE_TTL` (1h) **per outage, per machine** — not per session, not per dispatch — stamped in the outage record next to the email's own stamp. |
| root session blocked, no parent (`prompt_router`) | `escalation` | By design nothing can route it; the session is stalled until a human answers. Once per hour **per distinct prompt** (keyed on the prompt hash, so a redraw doesn't re-fire). |
| usage-limit park | `note` | **Demoted on purpose.** Self-healing: reset time parsed, resume nudge armed, the owner's email literally ends "no action needed". Real news, nothing to act on inside thirty seconds. One per park (`is_parked` is the throttle). |
| dead-lettered load-bearing mail | `request`, promoted to `escalation` iff the lost message *was* an escalation | Someone must go look at `agentwire msg dead`. The floor is `request` because the realistic bad case is one stuck recipient — 147 dead letters in ~2s, observed — and that shape must not buy 147 interrupts. One alert per **batch**, matching the digest email's coalescing. |
| dangling PR (`worktree --dangling`) | **not wired** | No autonomous trigger (only `doctor` and the explicit flag, both run by a human already reading the output) and no per-finding throttle state to reuse, so a producer would re-announce the same durable, passive condition every invocation. Nothing is burning while a dangling PR waits. |

### Lifecycle joins the same table (#1016)

Detectors report that something is *wrong*. `agentwire/fleet_activity.py` adds
the other producer — the fleet reporting that something *happened*: a session
going idle, a scheduled task finishing, a toast, anything spoken through fleet
TTS. It shares `DETECTOR_KINDS` because the question is identical (what may a
producer put in front of a listener, and how loudly), and answering it in two
places is how the two answers drift.

| Event | Kind | Why, and what bounds it |
|---|---|---|
| session went idle | `done`, **only if delegated** | The event the owner wanted: work they handed off has finished. Authority is consulted first and can veto — an `orchestrator`/`anchor` role is never delegated whatever its location, because `agentwire orchestrator` is `worktree --kind orchestrator` and an OR over #716's axes let *location* overrule the role, announcing the owner's own durable window every 15 minutes. After that veto, a recorded parent, a worker/reviewer role, or a worktree checkout each suffice. Throttled 15m per session; the event's parent is excluded, since it hears this by paste from `notify-parent`. |
| scheduled task completed | `done`, `request` if it ended badly | The owner did not watch it start and cannot see it end. The `request` promotion is the dead-letter inherit rule's shape: the fleet already judged it. `usage_limit` / `auth_expired` runs are **not** announced — those conditions have detectors of their own that say it once, machine-wide. Throttled 2m per task. |
| `notify-user --priority high` | `request` | The one notify surface that declares its own urgency, about a screen the owner may not be looking at. Throttled 5m per **toast content**, not per sender: keyed on the session, a second, different high toast a minute later was silently never spoken. |
| ordinary toast, portal lifecycle, **anything spoken** | **not wired** | Ledger-only. `spoke` most deliberately: the owner heard it in the room, and a voice channel that reads the audio back is worse than one that stays quiet. |

**Do not `alerts subscribe` a DELEGATED session.** Subscription is for a
listener, and a subscribed *worker* closes a loop: the alert is pasted into its
prompt, which starts a turn, which ends in an idle, which is announced — a
cycle that sustains itself at the 15m cadence with nobody asking for any of it.
Nothing autonomous does this (the buddy subscribes itself, and it has no pane
to paste into); it takes a human running `alerts subscribe` on a worker. The
subscriber you want is the one that *reads* mail rather than acting on it.

**No lifecycle event is ever an `escalation`.** The interrupt tier stays the two
conditions nothing clears without a human. What everything else gets is the
**ledger** — `~/.agentwire/fleet-activity.jsonl`, read with `agentwire activity
list`, never pushed at anyone. That split is the design, and it exists because
everything in a spool is eventually spoken; see
[voice-layer.md](../voice-layer.md#fleet-awareness-two-tiers-because-everything-in-the-spool-gets-spoken-1016).

**No alert rides an email-shaped throttle.** Each producer stamps its own state
on a successful *enqueue* — a local write — rather than reusing the stamp that
gates its owner email. That distinction is load-bearing rather than fussy:
`channels/email.py` **raises** `EmailConfigError` when `RESEND_API_KEY` is
absent, so on a keyless machine (the ordinary state of a fresh install) the
email-shaped gates never close at all, and anything riding one re-fires on every
60s watchdog tick. Note what this claim does *not* say: three email callers
(`auth_expired._escalate`, `prompt_router._escalate_no_parent`,
`usage_limit._send_notification`) still gate persistent state on a successful
send. That is untouched and out of scope here — but it is why a future producer
must not be wired to `notified`, `escalated_at` or their siblings.

A stamp also records that somebody **was told**, never that we tried: when an
alert reaches no subscriber, no stamp is written. Otherwise an operator who
subscribes during a live incident would hear nothing until the TTL expired,
which is the failure that is hardest to notice — it looks exactly like a quiet
fleet.

Two guards keep the dead-letter mirror from feeding itself: alerts carry a
distinct sender (`fleet-alerts` — or `fleet-activity` for lifecycle; both live
in `MACHINE_SENDERS`, and `emit` refuses any other) and are never mirrored, and
any subscriber named as a *recipient* of the lost batch is excluded. Either alone is
insufficient — with two subscribers, an alert stranded en route to one would
otherwise be reported to the other, once per drain, forever.

### What an escalation does NOT buy: speed

`escalation` is a **priority** statement, not a latency one, and the difference
is worth stating because the word invites the other reading. An alert is
ordinary inbox mail: it is written to the recipient's inbox immediately and then
waits for the drain, which rides the watchdog at `TICK_INTERVAL = 60`s. So
**up to ~60s passes before a subscriber sees an escalation at all**, before that
consumer applies whatever gating of its own it has. What the kind buys is what
happens *after* it arrives — a consumer may act on an escalation out of turn,
where it would make anything else wait for a gap.

The practical bar for the table above is therefore: *would this still be worth
someone's interruption a minute or two after it fired?* An event that is only
urgent in its first few seconds is not served by this channel at all, and an
event that will still matter in an hour belongs in `note`.

Rulings live as data in `fleet_alerts.DETECTOR_KINDS` and are pinned by
`tests/unit/test_fleet_alerts.py`, so changing what may interrupt is a
deliberate edit to a test that says why.

### CLI

```bash
agentwire alerts subscribe <session>     # lease alerts for a session
agentwire alerts unsubscribe <session>
agentwire alerts list [--json]           # live leases, with their expiry
agentwire alerts reindex [--json]        # rebuild the candidate index

agentwire activity list [--limit N] [--hours N] [--event E] [-s SESSION] [--json]
```

`activity list` reads the ledger — the awareness tier, which nothing pushes.
Entries marked `*` were *also* queued to subscribers; showing that flag is the
point, since "recorded" and "said out loud" are different facts and an operator
debugging a chatty or a silent buddy needs to tell them apart. There is
deliberately **no verb that writes an entry**: producers record from inside the
surfaces that generate the events, so nothing an agent can be talked into
calling can forge fleet history.

`list` reports the **expiry**, not a boolean, because a lease that stopped being
renewed stops delivering silently — that is the designed failure direction, and
it is one an operator has to be able to see.

`reindex` exists because the emit path deliberately never walks the session
record store: that walk measured ~326ms against 1155 records, and one caller
sits on the synchronous permission-hook path. The index names *candidates* and
each candidate's own record decides, so a stale entry is verified away and a
lost index costs alerts (never spurious ones) until this rebuilds it.

**The residual, stated because it is the one silent stop left:** a lost index
zeroes alerts quietly, and `list` reads that same index — so the surface meant
to reveal the stop would otherwise agree with it. `list` therefore reports
`index_present` and refuses to render a missing index as a confident zero. It
cannot do better than that: a machine where nobody ever subscribed also has no
index, and from the outside those two states are identical. `reindex` settles
it, cheaply, in both directions.

`subscribe` requires the session to already have a record. It used to create one
— a `{}` entry in the store that is the SSOT for conversation identity (#871),
counted by `core.recorded_sessions()` and indistinguishable from a real session.
A subscription is a property *of* a session, so one it can invent is not a
subscription.

## Broadcast

`--to @all` fans out to every **live agent session except the sender** — useful
for multi-worktree fan-out. Service sessions (portal, scheduler, TTS) are
skipped automatically because their pane 0 doesn't run an agent.

## CLI

```bash
agentwire msg send --to <session|@all> [--kind note|done|request|escalation|ingest|voice] [--ref <path>] <text | --body-file PATH>
agentwire msg send --to agentwire-dev-fix-nav --kind done "PR #312 drafted"
agentwire msg send --to anchor --kind ingest --ref /path/report.md "auth findings"  # passive
agentwire msg send --to reviewer --kind request --body-file /tmp/findings.md       # code-bearing body, no escaping
git diff --stat | agentwire msg send --to orch --body-file -                       # '-' reads stdin
agentwire msg inbox [-s <session>]   # peek pending + passive (does not drain/consume)
agentwire msg pull  [-s <session>]   # read + REMOVE passive (ingest) messages
agentwire msg dead  [-s <session>]   # list dropped (dead-lettered) msgs + why
agentwire msg dead  --purge [-s <session>] [--older-than 7d]  # clear the graveyard
agentwire msg flush [-s <session>] [--force]  # attempt a drain now (gated unless --force)
agentwire msg purge [<session>]      # drop a session's PENDING queue (self-heal a wedged inbox)
```

`--body-file PATH` (`-` for stdin, same shape as `gh --body-file`) exists
because a body **about code** carries backticks and `$(...)`, which the
caller's shell executes as command substitution before `msg` ever sees the
text — a word silently vanishes and the send still reports `Queued` (#944).
Mutually exclusive with positional text. `notify-parent` takes the same flag;
`council ask` already had `--file`/stdin. The MCP `msg_send` tool needs no
equivalent: it passes structured arguments and never transits a shell, which
is why the two paths differ in risk.

`msg send` to a named session that doesn't currently exist still queues (the
session may be about to be created) but **warns in the confirmation** — text
output prints a `Warning:` line, JSON carries `missing` + `warnings` fields,
and MCP `msg_send` relays it — so a sender never mistakes "Queued" for
delivery-in-progress when the target is gone (#694). No warning fires for
`@all` (targets are live by construction) or when tmux is unreachable (no
positive knowledge).

`msg dead` without `-s` is **global** — it lists every session that has dead
letters, whether or not you run it from inside a session (#693: the old
current-session fallback made the global view unreachable from inside tmux,
which is exactly where every monitoring agent lives). `-s` scopes to one
recipient. Each line shows the kind, sender, died-at time, attempt count, and
the drop reason.

`msg dead --purge` deletes corpses (`doctor` surfaces them but never grows a
cleanup itself). `-s` scopes the purge to one session; **without `-s` it clears
every session's graveyard** — the same no-`-s`-means-global rule as the lister.
`--older-than <dur>` (`7d`/`12h`/`30m`/`2w`) clears only
corpses that died before the cutoff, so you can drop stale ones and keep recent
report-backs you haven't read. Pre-schema corpses (no `dead_ts`) count as
infinitely old.

`msg purge <session>` is the **self-heal escape hatch** (#621): it drops the
session's *pending* (undelivered) queue outright — no empty-box gate, no
delivery — so a wedged recipient can be un-stuck without hand-moving JSON files
(which the recipient's own Bash hook blocks via `rm`). It never touches `dead/`
or passive `ingest/`. `msg flush --force` is the complement: it force-drains the
pending queue *past* the empty-box gate (it may land mid-draft, so it's an
operator action; `--force` requires `-s` and never bypasses the `safe_deliver`
gone/parked/non-agent/live-dialog guards).

**GC on sender exit.** When a session is killed via `agentwire kill`, the drain
GCs that sender's still-pending outbound across every recipient inbox so exited-
sender report-backs don't accumulate: load-bearing kinds (`done`/`request`/
`escalation`) dead-letter (and escalate via the owner-email path); the rest are
dropped. Passive `ingest` is left for the recipient to pull.

`--from` defaults to the current session. All commands take `--json`. The CLI is
the single source of truth; the portal and MCP call it.

## MCP tools

- `msg_send(to, text, kind="note", ref="")` — polite peer update; delivers at the
  next safe boundary. `kind="ingest"` is passive (pull-only); `ref` is a typed
  pointer. Warns in the confirmation when the named recipient doesn't
  currently exist (dead-letters in ~5 min unless it appears).
- `msg_inbox(session=None)` — peek pending + passive messages (does not consume).
- `msg_pull(session=None)` — read + remove passive (`ingest`) messages.
- `msg_flush(session=None, force=False)` — force a drain of the driving queue
  (gated unless `force=True`, which requires `session`).
- `msg_purge(session=None)` — drop a session's pending queue (self-heal a wedged
  inbox); never touches `dead/` or passive `ingest/`.
- `msg_dead(session=None)` — list dead-lettered messages with their drop reason
  + timestamp. Omitted `session` is GLOBAL (every session that has any) even
  when called from inside a session; pass a name to scope.

**Rule of thumb for agents:** use `msg_send` for routine peer updates that
shouldn't interrupt; use `session_send` only when you need to forcibly drive a
session right now.

## State

| Path | Purpose |
|---|---|
| `~/.agentwire/inbox/<session>/*.json` | queued driving messages (filename = order) |
| `~/.agentwire/inbox/<session>/ingest/` | passive `ingest` messages — pull-only, drain never walks here |
| `~/.agentwire/inbox/<session>/dead/` | dead-lettered after the attempt cap |
| `~/.agentwire/inbox/<session>/.lock/` | mkdir-based per-session drain lock |
| `~/.agentwire/inbox/.tick.lock` | global flock guarding `tick()` |
| `~/.agentwire/inbox-events.jsonl` | audit log (enqueued/delivered/deferred/dead_letter) |
| `~/.agentwire/sessions/<session>/metadata.json` → `fleet_alerts` | the subscription lease (see Fleet alerts) |
| `~/.agentwire/fleet-alerts-events.jsonl` | audit log (subscribed/emitted/emit_failed) |

## Scope (v1)

Local sessions only — cross-machine delivery is deferred. There is no portal
surface for the inbox yet. `msg` does not replace or deprecate `session_send`.
