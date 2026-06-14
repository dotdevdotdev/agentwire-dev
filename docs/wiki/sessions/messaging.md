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

4. **Defer or drop.** If either gate fails, the messages stay put and their
   `attempts` counter bumps. After `MAX_ATTEMPTS` (40 ≈ 40 min of a permanently
   busy session) a message moves to `~/.agentwire/inbox/<session>/dead/` and a
   `dead_letter` event is logged — no infinite retry.

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

## Broadcast

`--to @all` fans out to every **live agent session except the sender** — useful
for multi-worktree fan-out. Service sessions (portal, scheduler, TTS) are
skipped automatically because their pane 0 doesn't run an agent.

## CLI

```bash
agentwire msg send --to <session|@all> [--kind note|done|request|escalation] <text>
agentwire msg send --to agentwire-dev-fix-nav --kind done "PR #312 drafted"
agentwire msg inbox [-s <session>]   # peek pending (does not drain)
agentwire msg flush [-s <session>]   # attempt a drain now (still gated)
```

`--from` defaults to the current session. All commands take `--json`. The CLI is
the single source of truth; the portal and MCP call it.

## MCP tools

- `msg_send(to, text, kind="note")` — polite peer update; delivers at the next
  safe boundary.
- `msg_inbox(session=None)` — peek pending messages (does not drain).

**Rule of thumb for agents:** use `msg_send` for routine peer updates that
shouldn't interrupt; use `session_send` only when you need to forcibly drive a
session right now.

## State

| Path | Purpose |
|---|---|
| `~/.agentwire/inbox/<session>/*.json` | queued messages (filename = order) |
| `~/.agentwire/inbox/<session>/dead/` | dead-lettered after the attempt cap |
| `~/.agentwire/inbox/<session>/.lock/` | mkdir-based per-session drain lock |
| `~/.agentwire/inbox/.tick.lock` | global flock guarding `tick()` |
| `~/.agentwire/inbox-events.jsonl` | audit log (enqueued/delivered/deferred/dead_letter) |

## Scope (v1)

Local sessions only — cross-machine delivery is deferred. There is no portal
surface for the inbox yet. `msg` does not replace or deprecate `session_send`.
