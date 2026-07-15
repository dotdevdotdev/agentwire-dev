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
| **Sweep** | ≤60s | Plan-approval, AskUserQuestion, permission (backstop) | Rides the usage-limit watchdog: `agentwire limits tick` runs `usage_limit.tick()` **then** `prompt_router.tick()` — a usage-limit dialog parks before the prompt sweep ever sees it |

The sweep only looks at Claude Code panes (`pane_current_command` of `node`/
`claude`/a version string) and uses real-capture-derived detectors with a
**liveness check**: a live dialog ends the screen at its hint footer; a pane
merely *displaying* a quoted dialog has its own input box below and never
matches. Screens containing `[PROMPT from ` (our own notification) are poison
and never match — that's the loop guard.

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
`route_deferred`, `no_parent`, `prompt_answered`, `route_failed`).

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

- **Parent never notified**: check `agentwire prompts status` and the events
  log. `no_parent` → the session has no creator metadata and no yml parent.
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
