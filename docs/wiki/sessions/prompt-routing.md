# Prompt routing — interactive prompts go to the parent session

> Living document. Update this, don't create new versions.

When a child/worker session hits an interactive gate — a permission
confirmation, a plan-mode approval (ExitPlanMode), an AskUserQuestion dialog —
the human paths (audio alert + portal dialog) still fire, **and** the
session's parent/orchestrator gets a text notification with enough context to
inspect and answer. No parent → behavior is exactly what it was before.

Issue #276. Core module: `agentwire/prompt_router.py`.

## Detection paths

| Path | Latency | Covers | How |
|------|---------|--------|-----|
| **Hook** | seconds | Permission prompts | `agentwire-permission.sh` POSTs to `/api/permission/{session}` with `pane_index` + `tmux_session`; the portal routes before waiting on the human |
| **Sweep** | ≤60s | Plan-approval, AskUserQuestion, resume-from-summary, permission (backstop) | Rides the usage-limit watchdog: `agentwire limits tick` runs `usage_limit.tick()` **then** `prompt_router.tick()` — a usage-limit dialog parks before the prompt sweep ever sees it |

The sweep only looks at Claude Code panes (`pane_current_command` of `node`/
`claude`/a version string) and uses real-capture-derived detectors with a
**liveness check**: a live dialog ends the screen at its hint footer; a pane
merely *displaying* a quoted dialog has its own input box below and never
matches. Screens containing `[PROMPT from ` (our own notification) are poison
and never match — that's the loop guard.

## The resume dialog (#905)

`claude --resume` on a conversation past its age + token thresholds opens a
menu before it does anything:

```
This session is 2h 47m old and 233.6k tokens.
Resuming the full session will consume a substantial portion of your usage limits.
We recommend resuming from a summary.
  ❯ 1. Resume from summary (recommended)
    2. Resume full session as-is
    3. Don't ask me again
```

**`kind=resume`.** This is the worst-behaved blocked state the fleet has hit,
and it is worth understanding *why* rather than just that it's handled:

- The agent process is running, so `pane_current_command` is the agent and
  **every liveness check reports healthy** — `worktree --list`, the idle
  handler, a fleet roll-up. Only reading the pane content reveals it.
- `safe_deliver` correctly refuses to paste into a live menu, so every message
  aimed at that session queues behind it. The session goes quiet in both
  directions at once.

Four sessions sat here after the #901 recovery — one about four hours, including
the session that owned the P0 — and "13 sessions recovered" was reported on
process-liveness alone. Not an incident-only path either: it fires on *any*
sufficiently large resume, and `agentwire restart` (#871) resumes in place.

Two details that are load-bearing rather than incidental:

- The detector anchors on the body sentence, taken from the **string literals
  in the shipped binary**, not from a screenshot. The title above it
  interpolates age and token count, and the option labels could be reworded.
- Age and token count go in `summary`, never in `question` — so they stay out
  of the content hash. A dialog that redrew with a ticking age would otherwise
  read as a NEW prompt on every sweep and re-paste into the parent every 60s.

## When there is no parent

A root session has no parent by design, so `status=no_parent` used to be
terminal: a marker was written and nobody was told, forever. Fine for a prompt
a human is sitting in front of; wrong for an unattended root orchestrator,
which is then stalled until somebody happens to look at the pane.

Now the **owner is emailed** — the same Resend wiring the dead-letter path uses
for load-bearing messages, best-effort, never able to break the sweep. The mail
carries the question, the option keys, and the exact `prompts answer` command
including the hash.

Rate-limited by `escalated_at` on the marker: first sighting emails, then at
most once per hour (`NO_PARENT_ESCALATE_TTL`) while the *same* prompt stays up.
The sweep re-routes a no-parent prompt every tick — nothing sets `notified_at`,
so it never short-circuits — so an unthrottled escalation would be 60 emails an
hour. Relatedly, `detected_at` is **carried forward** across those rewrites: it
used to be refreshed every pass, which made a pane blocked for four hours read
as seconds old to anything measuring the wait.

## Finding blocked sessions

`prompt_router.blocked_panes()` is the read-only view — detect and report,
never route, never write a marker, never answer. `agentwire doctor` renders it:

| `status` | Meaning |
|----------|---------|
| `unrouted` | Live dialog, **no marker at all** — the sweep isn't running (watchdog down) or the session is excluded. Nobody has been told. |
| `no_parent` | Root session; owner emailed. |
| `waiting` | Parent notified, not yet answered. |
| `deferred` | Parent pane wasn't safe to paste into; retrying. |

`stuck` (what doctor counts as an issue) is `unrouted`, or waiting longer than
`STUCK_PROMPT_AFTER` (25 min — long enough for one `RENOTIFY_TTL` cycle to have
gone unanswered). A prompt routed a minute ago is the system working, not a
finding.

Pane enumeration for both the sweep and this check goes through
`prompt_router.list_panes()`, which uses `list-panes -a` (server-wide, no
target). Two tmux behaviours it exists to keep nobody re-deriving:

- **`tmux display-message` does not fail on an unresolvable target** — it
  silently returns the ACTIVE pane, rc=0, with a plausible wrong answer. Never
  probe with it. (`capture-pane` *does* fail loudly; this is per-subcommand.)
- **A targeted `list-panes -t <session>` scopes to the ACTIVE WINDOW.** `-s` is
  what makes it session-wide, so without it the first row is the wrong pane
  whenever the agent isn't in the active window.
- Pane indices always come from tmux, never a constant: base-index ships as 0
  since #903, but windows created before it kept 1, so both are live at once.

## Parent resolution (precedence)

1. **Worker pane** (index > 0) → pane 0 of the same session.
2. **Creator**: `agentwire new` / `agentwire worktree` record the calling
   tmux session in `~/.agentwire/sessions/{name}/metadata.json` — but only
   by **default when the new session is in the caller's own project**
   (same git repo, checked via `git rev-parse --git-common-dir` so it
   survives linked worktrees); a worktree/session spawned into a genuinely
   different project defaults to a standalone root instead of nesting under
   the caller (#715). `--created-by <name>` forces a specific parent
   regardless of project (e.g. for closely related projects); `--created-by
   ''` forces standalone even within the same project. `agentwire kill`
   removes a recorded creator.
3. **`.agentwire.yml` `parent:`** field.
4. None → human-only, unchanged.

Depth-1 and local-machine only. Remote (`@machine`) parents are out of scope:
each machine's own watchdog sweeps its panes; cross-machine delivery falls
back to human-only.

**Idle notifications use the same resolution.** When a pane-0 session goes idle,
`idle-handler.sh` calls `agentwire notify-parent --on-idle --queued`, which
resolves the parent through the precedence above and then (#667) **enqueues the
report-back on the [polite msg inbox](messaging.md) as `kind=done`** instead of
direct-pasting: the drain's empty-box gate means a busy orchestrator defers the
message rather than accumulating unsubmitted `[NOTIFY …]` lines in its input
box, busy deferral carries no dead-letter penalty, and an undeliverable
report-back dead-letters + emails the owner instead of vanishing (the hook logs
CLI failures instead of discarding them). Non-queued `notify-parent` calls
still direct-paste via `safe_deliver`. Resolution itself is unchanged — so a worktree / `agentwire new` child whose
parent lives in **creator metadata** (not `.agentwire.yml`) now correctly pings
its spawner on completion. Earlier the idle hook read only the `.agentwire.yml`
`parent:` field and silently dropped the notification when it was empty.
`--on-idle` additionally suppresses the ping when the source is an infrastructure
**service** (`services.is_service_session` — portal/tts/stt/kokoro/scheduler, the
idle-nag bridge, custom services); those cycle active→idle constantly and aren't
delegated work. Worker-pane (`notify-parent` from pane > 0) and explicit `--to`
callers skip the service check.

## Delivery safety

Every delivery goes through `safe_deliver()` (also used by
`agentwire notify-parent`, which fixed the dead `agentwire alert` path):

- **target_dialog** — the target pane shows a live menu: paste + Enter would
  *answer it*. Deferred, retried next tick.
- **target_not_agent** — pane 0 runs a shell: pasted text would *execute*.
- **target_parked** — usage-limit parked; a paste would corrupt the resume.
- **target_gone** — session died.
- Sends are verified (`send_verified`): a silent tmux paste failure reports
  as undelivered and retries. Verification keys on the **full**
  whitespace-normalized message (#667), never a fixed-length prefix — so a
  pile of same-prefix drafts can't false-match — and a retry that finds its
  own copy already landed in the box retries only the *submit*, never pasting
  a duplicate.

The message itself is paraphrased — no `❯`, no option block, no dialog footer
text — so it can never be re-detected as a dialog.

## Answering (the race guard)

The notification tells the parent to answer **only** via:

```bash
agentwire prompts answer -s <session> --pane <n> --expect <hash> <key> [key...]
```

It re-captures the pane, re-detects the prompt, and compares the content hash
from the notification before sending any key. A human may have answered first
via the portal — first answer wins, the loser no-ops. The portal's own
respond keystroke is equally guarded (re-capture, skip if the dialog is gone)
and pane-aware.

Never answer with raw `send-keys`: a stray `1` types into the freed input
box, a stray `Escape` aborts the child's in-flight turn.

## Markers + dedupe

`~/.agentwire/prompt-router/{session}.{pane}.json`, presence-based:

- Dialog detected → routed once, marker written (sha256 of normalized
  kind+question+options — stable across pane-width re-wraps).
- Dialog gone on a later sweep → marker cleared (an identical future prompt
  re-notifies).
- Still unanswered after 10 min → re-notified (`RENOTIFY_TTL`).
- Hook-source permission markers keep the sweep out (the portal owns that
  prompt's lifecycle, ~6 min TTL).
- The **idle-handler honors markers**: a pane with a routed prompt pending is
  never summary-prompted or auto-killed.

Events log: `~/.agentwire/prompt-router-events.jsonl` (`prompt_routed`,
`route_deferred`, `no_parent`, `no_parent_escalated`,
`no_parent_escalate_failed`, `prompt_answered`, `route_failed`).

## CLI

```bash
agentwire prompts status                  # pending prompt markers
agentwire prompts tick                    # run one sweep now
agentwire prompts answer -s S --expect H 2   # guarded answer
agentwire prompts clear -s S --pane 1     # drop a marker
```

## Config

```yaml
prompt_router:
  enabled: true            # default
  exclude_sessions: []     # never route prompts from these sessions
```

## Troubleshooting

- **A session is alive but doing nothing**: run `agentwire doctor` — the
  blocked-prompt section reads pane content, which is the only thing that sees
  this. Process liveness cannot.
- **Parent never notified**: check `agentwire prompts status` and the events
  log. `no_parent` → the session has no creator metadata and no yml parent
  (the owner is emailed instead; see above).
  `route_deferred` with `target_dialog`/`target_not_agent` → the parent pane
  wasn't safe to paste into; it retries every tick.
- **Re-notification spam**: shouldn't happen (presence markers + sha256). If
  it does, the dialog text likely redraws with changing content — file it
  with the capture.
- **Dialog text drift** (new Claude Code version): detectors anchor on real
  captures in `tests/unit/test_prompt_router.py`; unmatched menu-like screens
  land in the usage-limit `unmatched_dialog` events. Re-capture and update
  the fixtures.

## Related

- [Usage-limit recovery](../usage-limit-recovery.md) — same watchdog, runs
  first each tick.
- [Polite messaging](messaging.md) — `agentwire msg` drains on the same
  watchdog tick, after this prompt-routing sweep.
