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
   *submit*, so a whole-send retry can never double the draft.

   **Pasted ≠ submitted (#689).** Three closures for the paste-lands-but-Enter-
   is-swallowed failure: **(a)** `message_on_scrollback` excludes the input-box
   region — a message still sitting in the box no longer reads as "on
   scrollback", so the drain can't unlink a pending file the recipient never
   received (an unparseable box counts as *not* on scrollback: keep pending).
   **(b)** `send_verified`'s Phase-2 confirm is strict before the first Enter —
   an unparseable busy box plus activity glyphs can no longer declare a message
   submitted with **zero** Enter keystrokes ever sent. **(c)** When the drain
   finds one of its own pending messages rendered in the recipient's box, it
   heals via `session_ready.finish_submit` — an **Enter-only** retry (never a
   re-paste, so the #621 dedup holds), unlinking only once submission confirms
   and otherwise deferring without penalty (`stuck_in_box`). As a last-resort
   backstop, the watchdog pane-sweep flushes a bare Enter on any idle pane
   whose box has held identical **machine-injected** text (`[MSG…`, `[NOTIFY…`,
   `[Pasted text…`) for two consecutive sweeps — human-looking drafts are never
   auto-submitted.

4. **Defer or drop.** If either gate fails, the messages stay put, their
   `attempts` counter bumps, and the defer `reason` (`box_not_empty`,
   `target_parked`, …) is stamped on each message. After `MAX_ATTEMPTS`
   (40 ≈ 40 min of a permanently busy session) a message moves to
   `~/.agentwire/inbox/<session>/dead/` carrying that reason + a `dead_ts`, and a
   `dead_letter` event is logged — no infinite retry. `msg dead` surfaces these
   so the drop is never silent.

   Two refinements keep the penalty honest:

   - **No-penalty busy reasons.** `target_busy` (the box can't be parsed — the
     agent is running a long command) and `queued_placeholder` (the box shows
     Claude Code's *"Press up to edit queued messages"* — the agent is generating
     with human-queued input) are *busy*, not refusals. They defer **without**
     bumping `attempts`, so a legitimately-busy session never burns a report-back
     toward dead-letter; the message waits and delivers once the box frees up.
     The placeholder is matched loosely, and only the *penalty* changes — a
     non-empty box is still never pasted into (see the collision detector below).
   - **Out-of-band escalation.** When a **load-bearing** kind (`done` / `request`
     / `escalation`) does dead-letter, the owner is emailed via the shared Resend
     wiring (the same channel usage-limit parking uses) so the loss is surfaced
     even if nobody runs `msg dead`. `note` is fire-and-forget and `ingest` never
     auto-delivers, so neither is escalated. Escalation is best-effort — a send
     failure is logged (`dead_letter_escalate_failed`) and never breaks the drain.

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
| `escalation` | needs attention |
| `ingest` | **passive** — awareness only; never auto-delivered (see below) |

An optional `--ref` carries a machine-readable pointer (e.g. a report path)
alongside the text, surfaced as a typed field rather than parsed out of prose —
ideal with `ingest`.

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

## Broadcast

`--to @all` fans out to every **live agent session except the sender** — useful
for multi-worktree fan-out. Service sessions (portal, scheduler, TTS) are
skipped automatically because their pane 0 doesn't run an agent.

## CLI

```bash
agentwire msg send --to <session|@all> [--kind note|done|request|escalation|ingest] [--ref <path>] <text>
agentwire msg send --to agentwire-dev-fix-nav --kind done "PR #312 drafted"
agentwire msg send --to anchor --kind ingest --ref /path/report.md "auth findings"  # passive
agentwire msg inbox [-s <session>]   # peek pending + passive (does not drain/consume)
agentwire msg pull  [-s <session>]   # read + REMOVE passive (ingest) messages
agentwire msg dead  [-s <session>]   # list dropped (dead-lettered) msgs + why
agentwire msg dead  --purge [-s <session>] [--older-than 7d]  # clear the graveyard
agentwire msg flush [-s <session>] [--force]  # attempt a drain now (gated unless --force)
agentwire msg purge [<session>]      # drop a session's PENDING queue (self-heal a wedged inbox)
```

`msg dead` with `-s` scopes to one session; outside a session it lists every
session that has dead letters. Each line shows the kind, sender, died-at time,
attempt count, and the drop reason.

`msg dead --purge` deletes corpses (`doctor` surfaces them but never grows a
cleanup itself). `-s` scopes the purge to one session; **without `-s` it clears
every session's graveyard** — purge deliberately does *not* fall back to the
current session the way the lister does, since a silent self-scope on a delete
is too sharp an edge. `--older-than <dur>` (`7d`/`12h`/`30m`/`2w`) clears only
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
  pointer.
- `msg_inbox(session=None)` — peek pending + passive messages (does not consume).
- `msg_pull(session=None)` — read + remove passive (`ingest`) messages.
- `msg_flush(session=None, force=False)` — force a drain of the driving queue
  (gated unless `force=True`, which requires `session`).
- `msg_purge(session=None)` — drop a session's pending queue (self-heal a wedged
  inbox); never touches `dead/` or passive `ingest/`.
- `msg_dead(session=None)` — list dead-lettered messages with their drop reason
  + timestamp (omit `session` to list every session that has any).

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

## Scope (v1)

Local sessions only — cross-machine delivery is deferred. There is no portal
surface for the inbox yet. `msg` does not replace or deprecate `session_send`.
