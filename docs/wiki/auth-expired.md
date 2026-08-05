# Expired-Login Detection

> Living wiki page. Update this, don't create new versions.

When Claude Code's credentials expire, it answers every prompt with a
**synthetic** turn and goes idle. The session is alive, the agent process is
running, the pane looks healthy, and no completion signal will ever arrive.
Before #906 nothing in agentwire could see that state.

Sibling of [usage-limit recovery](usage-limit-recovery.md), and the same
zero-LLM shape — but the opposite trade on where the signal comes from, for a
reason spelled out below.

## The failure it exists for

Measured, from #867's transcript
(`~/.claude/projects/-Users-dotdev-projects-agentwire-dev/4f90262b-….jsonl`):

| Time (UTC) | |
|---|---|
| `08:00:05.283` | `task_started` — `memory-manager` |
| `08:00:20.785` | the task prompt lands as a `user` message (20,433 bytes / 518 lines) |
| `08:00:20.800` | `assistant`: `Login expired · Please run /login` |
| `08:00:20.801` | `turn_duration: 16ms`. Transcript ends. |
| `10:00:22.513` | `task_completed` · `incomplete` · **7217s** |

```json
{"model": "<synthetic>",
 "content": [{"type": "text", "text": "Login expired · Please run /login"}],
 "usage": {"input_tokens": 0, "output_tokens": 0},
 "error": "authentication_failed", "isApiErrorMessage": true}
```

Zero tokens, no model call. The dispatch did everything right and the turn was
refused before anything ran. `ai-morning-briefing` hit the same outage four
hours later and burned the scheduler's full **14400s** ceiling. Two tasks, six
hours, and three investigation passes chasing a guardrail, a dispatcher, a
timeout and a paste race — because `incomplete — Timeout waiting for task
completion` describes the symptom and misleads about the cause.

`authentication_failed` appears **6 times in the entire local history**; four
of them are on 2026-08-04, between `08:00:20Z` and `12:16:04Z`.

## Why the transcript, and NOT the pane

The usage-limit detector reads pane text. This one deliberately does not.

Claude Code renders the expired-login message **inline, as an ordinary
assistant message** — not as a select-menu. So the rendered phrase also
appears in any pane that merely *quotes* the incident. The session that
investigated #867 had `Login expired · Please run /login` on screen all day.

A pane-text rule would buy that false-positive class, and a false positive
here **halts scheduling for the whole machine** — strictly worse than the hang
it replaces. The usage-limit dialog can be *proven live* (nothing renders
after a live menu); an inline message cannot.

The transcript's `error` field is a structured fact about a turn that actually
happened. That is what is keyed on — never the rendered text, and never
"any API error".

**Field-proven against two outages, with different wording.** Sweeping every
`isApiErrorMessage` row on disk and classifying by error value gives
`rate_limit` 16, `authentication_failed` 6, `server_error` 5 — all
`type: assistant`. Those 6 auth rows are **two separate incidents that render
differently**:

| Date | Claude Code | Rendered text |
|---|---|---|
| 2026-08-04 (4 rows) | 2.1.221 | `Login expired · Please run /login` |
| 2026-07-07 (2 rows) | 2.1.201 | `Not logged in · Please run /login` |

Both are fixtured, and both fire. A detector matching the rendered string
would have silently stopped working across that rewording — which has already
happened once in this codebase's own history.

It also sizes the risk from the other side: **all 16 `rate_limit` rows are
structurally identical** to an auth refusal (`type: assistant`,
`model: <synthetic>`, zero tokens, `isApiErrorMessage: true`), so they DO
reach the predicate. Only the error-value check stops a transient rate limit —
already handled correctly by [usage-limit recovery](usage-limit-recovery.md) —
from gating every scheduled task on the machine.

### What would break it

Rewording is survivable and proven. **Restructuring is not.** If a future
Claude Code version nests the field (under `message.error`, or as an
`error.type` object), `row_is_auth_failure` returns False and the detector
goes **quiet rather than loud** — a silent regression, the worst direction.
No test can catch that on its own, because the fixture is a snapshot of
today's shape.

The check to run when a Claude Code upgrade lands: confirm `error` is still a
top-level string on an api-error row. The `cc-dialog-drift` scheduled task is
the natural home for it.

## Recovery is a property of the signal

Only the **last** assistant turn in a transcript decides. A session that
auth-failed at 08:00 and took a real turn at 09:00 reads as healthy, with
nothing to reset. Two of the four 08-04 transcripts are exactly that shape
(11MB and 2.6MB files that auth-failed mid-run and recovered) — keying on
"does this file contain an auth failure" would have gated the fleet on them
forever.

## Finding the transcript

Two routes, both evidence rather than a guess:

1. **Recorded** — `~/.agentwire/sessions/<name>/metadata.json` carries
   `conversation_ids` + `cwd_at_launch` since #871, so the exact file is
   addressable.
2. **Touched** — files in the project's history dir written **since this
   attempt began**. Needed because `memory-manager`'s own record predates
   #871's enrichment and has no `conversation_ids` at all: a detector that
   only handled route 1 would have been blind to the very incident it exists
   for.

Deliberately NOT "the newest `.jsonl` in the directory" — the guess CLAUDE.md
warns against. Route 2 is scoped to one project dir *and* to the attempt's own
time window.

**The window anchor is load-bearing.** It is the moment the *attempt* began,
not the moment the completion wait starts. The refusal is recorded ~15 ms
after the prompt submits, while the wait is only entered after `send_verified`
confirms submission seconds later — anchoring at the wait puts the evidence
just *before* the window, and the detector misses the exact run it was built
for. `ensure` passes `transcript_since=attempt_started`; there is a regression
test for the two anchors differing.

## Machine-wide, not per-task

An expired login is not one task's problem — every subsequent dispatch hits
it. So one detection records a single outage at
`~/.agentwire/auth-expired/state.json`:

```json
{"detected_at": "…", "last_seen": "…", "sessions": ["memory-manager"],
 "transcript": "/…/conv.jsonl", "escalated_at": "…", "host": "…"}
```

- **Escalation** emails the owner **once** per `ESCALATE_TTL` (1h) while the
  outage persists — the same shared Resend wiring the dead-letter digest and
  #905's no-parent escalation use, not a third channel. Best-effort: a missing
  key or a provider failure never turns a fast, correct failure into a slow
  one.
  Only a **successful** send stamps `escalated_at`, so a persistently broken
  sender retries once per detection instead of once per TTL. Deliberate: a
  failed send that counted as delivered would lose the escalation outright,
  and losing it is worse than retrying it. The retry stops the moment one
  send lands.
- **`detected_at` is carried forward** across refreshes, so a four-hour outage
  doesn't read as seconds old. (Refreshing it was a real defect in the prompt
  sweep, fixed in #905.)
- **`OUTAGE_TTL` (30 min) bounds the gate.** A flag that gated forever would
  take the whole board down on a stale file. Past the TTL one dispatch is let
  through as a probe — which now fails in seconds instead of hours.
- **A successful task completion clears the record outright.** A written
  summary is proof a turn ran, which is proof the login works, so
  `wait_for_completion_signal`'s success path calls `clear_state()` rather
  than making the fleet wait out the TTL. This is what makes "reopens on the
  first successful turn" — printed by `doctor` and by the escalation email —
  a fact instead of a description of behavior nothing implemented.

## Surfaces

| Surface | Behavior |
|---|---|
| `completion.wait_for_completion_signal` | returns `status=auth_expired` with a named reason instead of polling |
| `agentwire ensure` | exits **8** (`ENSURE_EXIT_AUTH_EXPIRED`); **never retries** — every retry refuses identically |
| scheduler dispatch | skips above the worktree/in-place fork; `last_status=auth_expired`, `last_run` **not** consumed, so the task is eligible the moment `/login` runs |
| worktree finalize | no PR — the worktree is untouched because nothing ran |
| `agentwire doctor` | reports a fresh outage with its evidence and the fix |

## Why there is no credential pre-flight

#906 asked whether a cheap check before spending a session launch would be
better than detecting after the fact. Weighed and rejected:

- On macOS the credentials live in the **Keychain**; reading them requires
  `security find-generic-password -w`, which raises an interactive
  authorization prompt. Unattended, that is a *worse* hang than the bug.
- Where a plaintext `~/.claude/.credentials.json` exists, its `expiresAt` is
  the **access** token's. That lapses routinely in normal operation and is
  refreshed silently, so gating dispatch on it would false-alarm constantly.
  "Login expired" means the *refresh* failed — which the local timestamp
  cannot distinguish.

The outage state above **is** the cheap pre-flight: one local file read, no
network. It costs one detection to arm, and then every later dispatch fails in
milliseconds.

## Diagnosing

```bash
agentwire doctor                                   # reports a fresh outage
cat ~/.agentwire/auth-expired/state.json           # the record
cat ~/.agentwire/auth-expired-events.jsonl         # detections + escalations
```

Fix: run `/login` in any Claude Code session. Nothing else is needed — the
first successful task completion clears the record, and the gate self-expires
within `OUTAGE_TTL` even if nothing completes.

## Related

- [Usage-limit recovery](usage-limit-recovery.md) — the sibling subsystem
- [Conversation identity](sessions/conversation-identity.md) — why the
  transcript is addressable at all (#871)
- #867 (the incident), #889/#890 (the theory this replaced), #906
