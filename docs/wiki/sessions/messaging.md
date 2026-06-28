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
   `[MSG from <sender> · <kind>] <text>`.

4. **Defer or drop.** If either gate fails, the messages stay put, their
   `attempts` counter bumps, and the defer `reason` (`box_not_empty`,
   `target_parked`, …) is stamped on each message. After `MAX_ATTEMPTS`
   (40 ≈ 40 min of a permanently busy session) a message moves to
   `~/.agentwire/inbox/<session>/dead/` carrying that reason + a `dead_ts`, and a
   `dead_letter` event is logged — no infinite retry. `msg dead` surfaces these
   so the drop is never silent.

### `prompt_is_empty` — the collision detector

The one genuinely new building block (`prompt_router.prompt_is_empty`). It reads
the bottom of the target pane with `capture-pane`, finds the Claude Code input
box (the region between the last two `─` rule lines), strips the `❯` glyph, and
returns `True` only if what remains is empty.

It is **conservative by design**: any non-empty content (a human draft *or* a
busy-state placeholder like "Press up to edit queued messages") and any screen
it can't parse as a clean empty box return `False`. A delayed message is fine; a
clobbered draft is not.

## Typed messages

`--kind` is a small enum (Overstory-inspired), not a workflow engine:

| kind | meaning |
|---|---|
| `note` | default — informational |
| `done` | a worker finished |
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
agentwire msg flush [-s <session>]   # attempt a drain now (still gated; never touches passive)
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

`--from` defaults to the current session. All commands take `--json`. The CLI is
the single source of truth; the portal and MCP call it.

## MCP tools

- `msg_send(to, text, kind="note", ref="")` — polite peer update; delivers at the
  next safe boundary. `kind="ingest"` is passive (pull-only); `ref` is a typed
  pointer.
- `msg_inbox(session=None)` — peek pending + passive messages (does not consume).
- `msg_pull(session=None)` — read + remove passive (`ingest`) messages.
- `msg_flush(session=None)` — force a (still-gated) drain of the driving queue.
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
