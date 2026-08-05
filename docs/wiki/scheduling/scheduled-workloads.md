> Living document. Update this, don't create new versions.

# Scheduled Workloads

Reliable headless task execution for unattended and automated agent workflows.

---

## Overview

Two paths for running scheduled work, picked per task:

1. **`agentwire ensure` tasks** — Reliable session management + task execution with lifecycle hooks. A full Claude Code session runs a single prompt from `.agentwire.tasks.yml`. Best for multi-step agent work that needs its own branch / PR / MCP tools.
2. **Tasks in `.agentwire.tasks.yml`** — Named tasks with pre/prompt/post phases and branch management — the substrate for path #1.

Both are orchestrated by `~/.agentwire/scheduler.yaml` and the AgentWire scheduler daemon.

---

## Task Definition Schema

Define tasks in `.agentwire.tasks.yml` (a sibling of `.agentwire.yml`, split out in #720) — **keep that file gitignored**. Worktree-dispatched runs check out HEAD, so if the file is tracked, uncommitted live edits to a task prompt are silently ignored and the run executes the stale committed version. Gitignored, the live file is seeded into every worktree via `projects.worktrees.copy_files` (default `[".env", ".agentwire.yml", ".agentwire.tasks.yml"]`) and always wins. See the `agentwire-project-config` skill.

`.agentwire.tasks.yml` is **protected control-plane** — a policed agent can't write it directly. Authoring it is propose-and-promote: draft to `.agentwire.tasks.proposed.yml`, then a human runs `agentwire tasks review` and `agentwire tasks promote`. See [Damage control](../internals/damage-control.md#task-execution-config-split-agentwiretasksyml-720).

**Legacy inline tasks are dead weight (#736).** Tasks that predate the #720/#721 split still living under a `tasks:` key in `.agentwire.yml` do **not** run — the executor reads only `.agentwire.tasks.yml`, with no runtime fallback. Migrate once: `agentwire tasks migrate` stages the inline block to `.agentwire.tasks.proposed.yml`, then `agentwire tasks review` + `agentwire tasks promote` lands it; finally delete the dead `tasks:` block from `.agentwire.yml`. `agentwire doctor` flags any project still in the un-migrated state.

```yaml
# .agentwire.yml — declarative session config
posture: auto    # Recommended for unattended work — see auto below
roles:
  - task-runner
```

```yaml
# .agentwire.tasks.yml — task-execution config
shell: /bin/sh       # Default shell for task commands

tasks:
  write-tests:
    # Execution control
    shell: /bin/bash         # Override shell for this task
    retries: 2               # Retry on failure (default: 0)
    retry_delay: 30          # Seconds between retries (default: 30)
    idle_timeout: 60         # Seconds of idle before completion (default: 30)
    max_duration: 1800       # Hard wall-clock ceiling per attempt (default: 0 = unbounded)
    exit_on_complete: true   # Exit session after completion (default: true)
    role: piinpoint-test-writer  # Role override for this task (optional)

    # Branch management (for autonomous workflows)
    starting_ref: main       # Git ref to checkout before task runs
    work_branch: agent/task  # Branch for agent's work (default: agent/<task>-<date>)
    pr_target: main          # PR target branch (default: starting_ref)
    pr_draft: true           # Create as draft PR (default: true)
    allow_shared_dir: true   # Attach even if another session sits in the project
                             # dir (default: derived — see below)

    # Context inheritance
    starting_session: ctx-loaded  # Fork Claude context from this session before running

    # Data gathering (produces variables for use in prompt)
    pre:
      weather: "curl -s wttr.in/?format=3"
      calendar:
        cmd: "gcal-cli today --json"
        required: true          # Fail task if output is empty
        validate: "jq . > /dev/null"  # Fail task if command exits non-zero
        timeout: 30             # Fail task if takes longer than 30s

    # Main prompt (supports {{ variables }})
    prompt: |
      Weather: {{ weather }}
      Calendar: {{ calendar }}
      Write tests for the payments module.

    # Optional: final prompt after system summary
    on_task_end: |
      Read {{ summary_file }}.
      If complete, push your work.

    # Post-task commands (runs after completion)
    post:
      - "echo 'Status: {{ status }}'"

    # Output handling
    output:
      capture: 50                    # Lines to capture from session
      save: ~/logs/{{ task }}.log    # Save captured output here
```

---

## Branch Management

When `starting_ref` is set, the task lifecycle handles all git plumbing automatically:

**Pre-task:**
1. `git checkout starting_ref` (+ `git pull --ff-only` if it's a branch)
2. Create `work_branch` (default: `agent/<task>-<YYYY-MM-DD>`, auto-deduped if exists)
3. `git checkout -b work_branch`

**Post-task:**
1. Commit any uncommitted changes: `git add -A && git commit -m "chore: agent task <task>"`
2. `git push -u origin work_branch`
3. Open PR: `gh pr create --base pr_target --head work_branch [--draft]`
4. PR URL is stored in summary file (available as `{{ pr_url }}` in post phase)
5. `git checkout starting_ref` — reset working state

**Edge cases:**
- `starting_ref` not found → task fails (exit code 4)
- No changes after task → no commit, no push, no PR (graceful skip)
- `gh` not in PATH → warning logged, task continues without PR

**Variables available in post phase when branch management is active:**

| Variable | Description |
|----------|-------------|
| `{{ work_branch }}` | Branch name used for agent's work |
| `{{ pr_url }}` | URL of the created PR (empty if no PR was created) |

### Sharing a working dir with a live session (#854)

`agentwire new` refuses to attach a session to a directory that is already some
other live session's working dir — two agents in one tree means dirty state
bleeding across and branches mixing. That guard is written for the *accidental*
case; a scheduled dispatch is declared intent (the task config names the
project, on a schedule, on purpose), so `ensure` opts out of it — but only when
the dispatch does no branch work of its own.

The derivation keys off `starting_ref`, since that is the field that makes
`ensure` check out, branch, and reset the tree:

| Task | Guard | Why |
|------|-------|-----|
| no `starting_ref` | **off** — dispatch proceeds | branchless: fans out, writes files, opens no branches |
| `starting_ref` set | **on** — dispatch refuses | mutates the shared checkout; use a worktree task instead |

Set `allow_shared_dir` explicitly to override in either direction — `false`
re-arms the guard for a branchless task whose *prompt* does git work, `true`
opens it for a `starting_ref` task whose tree is known to be private. The
dispatch never uses `--force` for this: force would kill-replace a live
same-name session.

---

## Context Inheritance

`starting_session` forks a session's Claude conversation history into the task session before running, giving the agent pre-loaded context instead of a cold start:

```yaml
tasks:
  continue-payments-refactor:
    prompt: "Continue the payments refactor from where we left off"
    starting_session: payments-loaded   # Fork Claude context from here
    starting_ref: feature/payments      # Also start from this branch
```

When the task runs, `payments-loaded`'s Claude conversation JSONL is copied into the new session. The agent starts with full prior context.

**Fallback:** If `starting_session` doesn't exist, a warning is logged and the task runs with a fresh session — not a hard failure.

---

## Per-Task Role Override

`role` loads a specialized persona for that task, overriding the session's default roles:

```yaml
tasks:
  write-tests:
    prompt: "Write unit tests for the payments module"
    role: piinpoint-test-writer    # Specialized test-writing persona

  lint-cleanup:
    prompt: "Fix all lint errors"
    role: task-runner              # Minimal, focused persona

  pr-review:
    prompt: "Review the open PRs and leave detailed comments"
    role: code-reviewer            # Review-oriented persona
```

Role applies at session creation time. With `exit_on_complete: true` (default), each task run creates a fresh session with the specified role loaded.

---

## Built-in Variables

| Variable | Available In | Description |
|----------|-------------|-------------|
| `{{ var_name }}` | prompt, on_task_end, post | Output from pre command |
| `{{ summary_file }}` | on_task_end, post | Path to current run's summary file |
| `{{ output }}` | post | Captured session output |
| `{{ status }}` | on_task_end, post | `complete`, `incomplete`, or `failed` |
| `{{ summary }}` | on_task_end, post | One-line summary from summary file |
| `{{ work_branch }}` | post | Branch created by branch management |
| `{{ pr_url }}` | post | PR URL created by branch management |
| `{{ date }}` | all | YYYY-MM-DD |
| `{{ time }}` | all | HH:MM:SS |
| `{{ datetime }}` | all | Full ISO timestamp |
| `{{ session }}` | all | Session name |
| `{{ task }}` | all | Task name |
| `{{ project_root }}` | all | Absolute path to project directory |
| `{{ attempt }}` | prompt, on_task_end, post | Current attempt number (1-based) |

`pre:` commands **produce** variables — they cannot use `{{ }}` syntax.
`prompt`, `on_task_end`, and `post` **consume** variables.
Environment variables use `${ENV_VAR}` syntax (expanded at runtime).

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Complete (`status: complete`) |
| 1 | Failed (`status: failed`) |
| 2 | Incomplete (`status: incomplete`) |
| 3 | Lock conflict (session locked, `--wait-lock` not used) |
| 4 | Pre-phase failure (command failed, `required` empty, `validate` failed, `starting_ref` not found) |
| 5 | Timeout (hard timeout exceeded) |
| 6 | Session error (couldn't create or connect) |
| 7 | Usage limit (`usage_limit` — session parked awaiting reset; scheduler skips dispatch) |

---

## `ensure` Command

```bash
agentwire ensure -s session --task name                        # Run named task
agentwire ensure -s session --task name --lock-timeout 600      # Custom lock-wait timeout
agentwire ensure -s session --task name --wait-lock             # Wait if locked
agentwire ensure -s session --task name --dry-run               # Preview without executing
```

**Lifecycle:**
1. Acquire lock for session (fail if locked, or wait with `--wait-lock`)
2. Session exists? If not, create it
3. Session healthy? If not, recreate it
4. Session idle? If not, wait
5. If `starting_session` set: fork Claude conversation context
6. If `starting_ref` set: checkout branch, create work branch
7. Run pre-commands, validate outputs
8. Send templated prompt
9. Agent works → goes idle → system sends summary prompt
10. Agent writes `.agentwire/task-summary-{session}-{task}-{datetime}.md`
11. If `on_task_end` defined: send user's final prompt, wait for idle
12. If `starting_ref` set: commit changes, push, open PR
13. Run post-commands with `{{ status }}`, `{{ pr_url }}`, etc.
14. Release lock, exit session

---

## Scheduler Integration

> **`scheduler.yaml` holds your task definitions only.** The daemon treats it
> as read-only and never writes to it. Run-state (last run, status, summaries,
> gate commits, worktree/PR tracking) is persisted to a separate
> `~/.agentwire/scheduler-state.yaml`, written atomically with rotated backups
> (`scheduler-state.yaml.bak1..bak5`). This keeps machine-written state from
> ever corrupting hand-authored tasks.

Schedule tasks in `~/.agentwire/scheduler.yaml`:

```yaml
tasks:
  nightly-tests:
    project: ~/projects/piinpoint
    session: piinpoint-tests
    task: write-tests
    posture: auto              # Posture override
    roles: [task-runner]
    once: true                 # Auto-disable after first run
    schedule:
      every: 1m
      not_before: "22:00"
      not_after: "06:00"

  nightly-lint:
    project: ~/projects/piinpoint
    session: piinpoint-lint
    task: lint-cleanup
    posture: auto
    schedule:
      after: nightly-tests
      delay: 2m

  morning-report:
    project: ~/projects/piinpoint
    session: piinpoint-report
    task: morning-report
    schedule:
      after: [nightly-tests, nightly-lint]
      delay: 5m
    post:
      - "agentwire scheduler report --since 12h --artifact"
```

### One-Time and Limited Tasks

Tasks can auto-disable after a set number of runs:

```yaml
tasks:
  tonight-scaffold:
    # ...
    once: true        # Run once, then auto-disable (shorthand for max_runs: 1)
    schedule:
      every: 1m

  quarterly-report:
    # ...
    max_runs: 4       # Run 4 times then auto-disable
    schedule:
      every: day
      at: "09:00"
```

- `once: true` — shorthand for `max_runs: 1`
- `max_runs: N` — auto-disables after N dispatches, logs `task_disabled` event
- Re-enable with `agentwire scheduler enable <task-name>`

---

## Morning Dashboard

After unattended tasks run, generate a summary report:

```bash
agentwire scheduler report --since 8h           # Print summary + artifact path
agentwire scheduler report --since 8h --artifact  # Also open in portal
```

The HTML report includes: task name, status badge, branch, PR link, duration, and one-line summary. PR URLs are populated automatically when tasks use `starting_ref` + `work_branch`.

---

## `auto` — Recommended Posture

For unattended work, use `auto` instead of `bypass`:

```yaml
# .agentwire.yml
posture: auto
```

`auto` uses Claude Code's auto mode: a background Sonnet 4.6 classifier reviews each tool call before execution. Safe actions (file reads, edits, git ops) run immediately with no overhead. Dangerous actions (force push to main, mass deletion, credential exfiltration) are blocked.

`bypass` has no safety checks. `auto` does everything `bypass` does for normal unattended work but prevents catastrophic failures at 3am when nobody's watching.

**Requires:** Team or Enterprise Claude plan. Pro/Max individual plans not supported.

See `../sessions/claude-code-auto-mode.md` for full setup, allow rule configuration, and constraints.

---

## Full Unattended Workflow Example

```yaml
# ~/projects/piinpoint/.agentwire.yml
posture: auto
roles:
  - task-runner
```

```yaml
# ~/projects/piinpoint/.agentwire.tasks.yml
tasks:
  write-tests:
    prompt: "Write missing unit tests for recent changes in the payments module. Focus on edge cases."
    starting_ref: main
    pr_target: main
    pr_draft: true
    role: piinpoint-test-writer
    retries: 1
    idle_timeout: 60
    exit_on_complete: true

  lint-cleanup:
    prompt: "Run the linter, fix all auto-fixable issues, commit the fixes."
    starting_ref: main
    pr_target: main
    pr_draft: false
    role: task-runner
    exit_on_complete: true

  morning-report:
    prompt: "Summarize what was accomplished. Check the PRs that were opened."
    post:
      - "agentwire scheduler report --since 12h --artifact"
    exit_on_complete: true
```

---

## When to Use What

| Workflow | Tool | Best For |
|----------|------|----------|
| Predefined recurring tasks | **Scheduler** | Nightly tests, lint, reports |
| Quick one-off tasks | **`agentwire ensure`** | Ad-hoc task execution |

```yaml
# ~/.agentwire/scheduler.yaml
tasks:
  nightly-tests:
    project: ~/projects/piinpoint
    session: piinpoint-tests
    task: write-tests
    posture: auto
    once: true
    schedule:
      every: 1m
      not_before: "22:00"
      not_after: "06:00"

  nightly-lint:
    project: ~/projects/piinpoint
    session: piinpoint-lint
    task: lint-cleanup
    posture: auto
    once: true
    schedule:
      after: nightly-tests
      delay: 2m

  morning-report:
    project: ~/projects/piinpoint
    session: piinpoint-report
    task: morning-report
    schedule:
      after: [nightly-tests, nightly-lint]
      delay: 5m
```

Each night: tests task and lint task each fork their own branch, do their work, and open a draft PR. Morning report runs after both, generates an HTML dashboard showing statuses and PR links.

---

## Daemon Liveness and the Single-Dispatcher Rule (#873)

**One board, one dispatcher.** Two `agentwire scheduler serve` processes against
the same `scheduler.yaml` double-dispatch tasks: the same task fires twice, the
first attempt usually times out, a later one completes, and the board shows both.

Liveness is determined from the daemon's own live-state file
(`~/.agentwire/scheduler-live.json`), which records the writing process's `pid`
on every loop tick. `live_daemon_state()` (`agentwire/scheduler/report.py`) is
the single source of truth: it returns the state only when that PID is alive
*and* its `ps` argv contains both `scheduler` and `serve` as whole words. A
leftover file from a stopped daemon therefore reads as not-running, and a
recycled PID can't masquerade as one.

The whole-word test on **both** words is load-bearing, not fussiness. A
recycled PID is quite likely to be another agentwire command: `agentwire
scheduler live --watch` is not a dispatcher, but a substring test for
`scheduler` accepts it — which would misreport liveness *and* make `serve`,
`start`, and portal autostart all refuse, leaving the board with **no**
dispatcher. Only `scheduler serve` dispatches.

This replaced `tmux_session_exists("agentwire-scheduler")`, which only ever knew
about daemons tmux itself hosts. A daemon under an external supervisor (launchd
`RunAtLoad` + `KeepAlive`) has no tmux session, so it:

- reported as `stopped` while it was actively dispatching, and
- caused `agentwire doctor` to **skip** the daemon-staleness check — the
  diagnostic that catches a wedged daemon — exactly where it was most needed.

Everything that asks "is the scheduler running" now routes through the same
check:

| Surface | Behavior |
|---|---|
| `agentwire scheduler status` | Reports `running (pid N, tmux \| external supervisor)` |
| `agentwire scheduler serve` | **Refuses to start** if a daemon is already live (`--force` overrides) |
| `agentwire scheduler start` | Refuses when a daemon is live outside tmux |
| `agentwire scheduler stop` | Says "running outside tmux — stop it through its supervisor" instead of the false "not running" |
| `agentwire doctor` | Runs the staleness check for tmux and non-tmux daemons alike |
| Portal autostart (`scheduler.autostart`) | Skips with a logged notice when any daemon is live, not just a tmux one |

`scheduler.autostart` still defaults to `true`. With the guard in place that is
safe alongside an external supervisor: the portal checks first and logs why it
declined, rather than silently adding a second dispatcher on every launch.

**One transitional state:** a daemon started before this change writes no `pid`,
so nothing can verify it. `doctor` flags that explicitly (`records no PID —
predates the PID-based liveness check`) rather than reporting it as stopped.
Restarting the daemon clears it — which a rebuild already requires anyway.
---

## Bounding a Task: `max_duration` (#867)

Completion is **agent-driven**. `ensure` sends the prompt and then waits for the
agent to go idle, the idle hook to prompt for a summary, and the summary file to
appear. Nothing in that chain has a clock. An agent that never goes idle — wedged
on an unrecognized dialog, or blocked inside a long tool call — produces no
completion signal and no error, so the wait simply never ends.

`max_duration` is the wall clock that bounds a single attempt:

```yaml
tasks:
  memory-manager:
    max_duration: 1800   # give up after 30 min (default: 0 = unbounded)
```

On expiry the attempt reports `incomplete`, the summary names the reason
(`Task exceeded max_duration (1800s) after 1802s — the agent never signalled
completion`), and the session is torn down — otherwise the wedged agent keeps
running until the scheduler's 4h `dispatch_max_runtime` process-group kill.

**It is a wall clock, not an idle timer.** It cannot distinguish a wedged agent
from a slow one, so a task that legitimately runs long should set it generously
or leave it at `0`. `idle_timeout` is *not* an equivalent bound: agentwire does
not control the harness's idle threshold, so that field configures the summary
handoff, not a ceiling on the run.

### Keys agentwire doesn't read are now reported

`max_duration` existed in `.agentwire.tasks.yml` files across the fleet long
before anything read it — a task that looked bounded at 30 minutes was in fact
unbounded. Unknown keys in a task block are now surfaced by `agentwire task
show` / `task validate` / `tasks review`, and warned about at dispatch:

```
Warning: task 'memory-manager' sets keys agentwire ignores: max_durationn
```

Reported, never fatal: a typo must not break a 04:00 dispatch. `description` is
allowed as a deliberate no-op annotation.
