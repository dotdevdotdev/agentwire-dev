# Usage-Limit Recovery

> Living wiki page. Update this, don't create new versions.

Deterministic recovery from the Claude Code usage-limit dialog. When a
session hits its usage limit mid-task, Claude Code presents an interactive
menu (`/rate-limit-options`) and the session blocks forever waiting for a
human — a silent deadlock for any unattended surface (scheduled tasks,
overnight queue, missions, worker panes). This subsystem detects the dialog,
parks the session, emails the owner, and nudges the session back to work
after the limit resets.

**Zero LLM involvement, by design.** At the moment this fires, usage is
exhausted by definition — no agent can run to compose an alert or schedule a
retry. The whole loop is plain code: pane-text regex via `tmux capture-pane`,
`send-keys`, a regex over the dialog's "resets 11:40pm (America/Toronto)"
line, a direct Resend call, and a launchd timer. The only agent in the story
is the parked session itself resuming when the nudge lands.

## The flow

```
dialog appears ──▶ detect ──▶ park (send-keys '1' Enter) ──▶ parse reset time
                                      │
                                      ▼
                          write ~/.agentwire/usage-limit/<session>.json
                                      │
                  ┌───────────────────┼─────────────────────┐
                  ▼                   ▼                     ▼
            email owner      guards everywhere      watchdog tick (60s)
            (Resend, direct) (never reaped/         resume_at passed?
                              re-dispatched)        send fixed nudge ──▶ session
                                                    archive to done/      finishes
```

Detection runs in two places:

| Where | Latency | Covers |
|-------|---------|--------|
| ensure's completion poll (`completion.wait_for_completion_signal`) | ≤10s | scheduler + overnight dispatched tasks |
| `agentwire limits tick` watchdog sweep over **all** tmux panes | ≤60s | everything else: missions, workers (panes 1+), interactive sessions |

Both call the same module: `agentwire/usage_limit.py`.

### Detection details

- Matches the distinctive option line **"Stop and wait for limit to reset"**
  plus the live-menu footer **"Enter to confirm"**, whitespace-normalized so
  narrow-pane line wraps can't break it.
- **Visible screen only** (no scrollback) and the menu must *end* the screen
  — a pane merely displaying a captured dialog (an orchestrator reviewing
  another session's output) has its own prompt below the quoted text and is
  not parked.
- Unknown live menus ("What do you want to do?" + "Enter to confirm" without
  the known option) are logged as `unmatched_dialog` events — the canary for
  dialog-text drift across Claude Code versions. If a real limit dialog ever
  stops matching, look here first.

### Reset-time parsing

`resets 11:40pm (America/Toronto)` → IANA zone via `zoneinfo`, rolled forward
to the next occurrence. If the stated clock time is more than one 5h window
away, the reset already passed → resume immediately. If nothing parses, fall
back to **now + 5h** (limits reset every 5h from window start, so that's a
guaranteed upper bound) and log `reset_parse_failed`. Resume fires at
`reset_at + 2min`.

## The parked state

`~/.agentwire/usage-limit/<session>.json` — file presence == parked. That is
the guard the rest of the system checks:

| Surface | Behavior while parked |
|---------|----------------------|
| `agentwire ensure` | exits immediately with code **7** (`usage_limit`) — never prompts a parked session |
| scheduler dispatch | skips the task (`task_skipped reason=usage_limit_parked`) without consuming `last_run`; no pre-dispatch session kill |
| scheduler status | ensure exit 7 → `last_status: usage_limit` on the board/events |
| idle hook (`idle-handler.sh`) | exits before any idle handling — no summary prompt typed into the dialog, no `/exit`, no kill |
| overnight `check_completion` | reports `running`, never `stuck` |
| `agentwire list --sessions` | `usage_limit: true` in JSON, `[parked: usage limit]` in text; flows to MCP `sessions_list` and the portal sessions API |

On resume the file is archived to `~/.agentwire/usage-limit/done/` with a
final status (`resumed` / `orphaned` / `resume_failed`), so completion then
flows normally through the idle hook.

The resume nudge is fixed text:

> You were interrupted by a usage limit; the limit has reset. Continue your
> task from where you stopped and complete it fully.

Delivery is verified by recapturing the pane; up to 5 attempts across ticks,
then `resume_failed` + an email asking for a human look.

## Configuration

`~/.agentwire/config.yaml`:

```yaml
usage_limit:
  enabled: true            # master switch for detection/parking (default true)
  exclude_sessions:        # sessions never auto-parked (default empty)
    - jordan
```

These knobs gate **new parks only** — both the watchdog sweep and ensure's
in-band detection. Already-parked sessions are always resumed and `agentwire
limits resume` always works, even for excluded sessions or with the feature
disabled (you can turn it off without stranding a parked session).

## CLI

```bash
agentwire limits tick          # one watchdog pass (what launchd runs)
agentwire limits status        # parked sessions + reset/resume times
agentwire limits resume -s X   # manual resume now (--force to skip verify)
agentwire limits install       # write + load the launchd watchdog (60s tick)
agentwire limits uninstall
```

The watchdog plist (`dev.agentwire.usage-limit-watchdog`) is generated by
`limits install` — no template file to drift. It runs 24/7 (limits don't
keep office hours). Logs: `~/Library/Logs/agentwire-usage-limit-watchdog.log`.
Events: `~/.agentwire/usage-limit-events.jsonl` (`session_parked`,
`notify_sent`, `session_resumed`, `unmatched_dialog`, `reset_parse_failed`,
`park_orphaned`, …).

## Notifications

Park and resume each send one email through the existing Resend channel
(`agentwire/channels/email.py::send_email`), pure code path. The park email
includes task, project, detected/reset/resume times, and the dialog excerpt.
If the send fails (no key in launchd's env, transient API error), it's
retried on subsequent ticks until it lands (`notified: false` in the state
file marks the pending retry).

## Known limitations

- **Worktree scheduler tasks**: a task parked mid-run skips the
  commit/push/PR finalize (half-done work must not be pushed). The session
  resumes and finishes via the idle hook, but the PR is **not** opened
  automatically — the worktree is left in place for manual follow-up.
- **Remote machines**: detection/park state is per-machine. The watchdog
  only sweeps local tmux; install it on each machine that runs unattended
  sessions.

## Troubleshooting

- Dialog not being parked? `tail ~/.agentwire/usage-limit-events.jsonl` —
  an `unmatched_dialog` event means Claude Code changed the dialog text;
  update the anchors in `agentwire/usage_limit.py` (`PARK_OPTION`,
  `MENU_FOOTER`) and the regex `_RESET_RE`.
- Watchdog not running? `launchctl list | grep usage-limit` and
  `tail -f ~/Library/Logs/agentwire-usage-limit-watchdog.log`.
- Session stuck parked after its session died? Ticks archive those as
  `orphaned` automatically; `agentwire limits status` should be empty.

## History

Born from the 2026-06-10 incident: two scheduler verification runs hit the
limit seconds after dispatch and sat on the dialog ~11 hours overnight —
the supervising worker was polling shells, the shells were waiting on
sessions, the sessions were waiting on a human. The manual recovery
(select option 1, nudge after reset) completed both runs green and is
exactly what this subsystem automates. Issue #274.
