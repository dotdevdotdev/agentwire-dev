---
name: agentwire-scheduler
description: Scheduler configuration and the overnight session queue — task gates (`git_commit`, `git_diff`, `command`), `schedule` field reference (duration vs calendar, `at`/`every`/`after`/`delay`/`cooldown`/`not_before`/`not_after`/`except`), priority/pipeline ordering, one-time/max_runs tasks, workflow-backed tasks (`workflow:` + `inputs:`), overnight prepare-and-dispatch workflow. Use when adding or debugging scheduled tasks in `~/.agentwire/scheduler.yaml`, wiring up overnight sessions, or explaining how gating/dispatch works.
---

# AgentWire Scheduler & Overnight Queue

## Scheduler Task Gates

Tasks in `~/.agentwire/scheduler.yaml` can define `gate` preconditions to skip execution when changes aren't relevant:

```yaml
tasks:
  code-quality:
    gate:
      git_commit: true  # Skip if HEAD unchanged since last run
  doc-drift:
    gate:
      git_diff:         # Skip if no commits touched these paths
        - docs/
        - agentwire/
  custom-check:
    gate:
      command: "test -f /tmp/ready.flag"  # Skip if command exits non-zero
```

Gates are evaluated before dispatching and skip the task (zero AI cost) if conditions fail. Multiple gates are AND'd. Gates fail open on errors.

## Scheduler Task Scheduling

Each task has a `schedule` field (replaces the old `interval`). The scheduler uses `_compute_next_eligible()` as the single source of truth for when a task becomes eligible.

**`schedule` field reference:**

| Key | Type | Description |
|-----|------|-------------|
| `every` | `str` | `30m`, `2h`, `1d` (duration) or `day`, `weekday`, `weekend`, `monday`..`sunday` (calendar) |
| `at` | `str` | Target time `"HH:MM"` local. Only with calendar `every` values |
| `except` | `list[str]` | Days to skip: `["saturday"]` |
| `after` | `str` | Dependency task name |
| `delay` | `str` | Wait after dependency: `"1h"`, `"30m"` |
| `cooldown` | `str` | Min time between runs: `"4h"` |
| `require_status` | `str` | `"complete"` (default) or `"any"` |
| `not_before` | `str` | Earliest time of day: `"08:00"` |
| `not_after` | `str` | Latest time of day: `"22:00"` |

Must have at least `every` OR `after` (or both).

**Examples:**
```yaml
# Duration-based interval
schedule:
  every: 4h

# Time-anchored daily
schedule:
  every: day
  at: "08:00"

# Dependency with delay
schedule:
  after: upstream-task
  delay: 1h
  cooldown: 3h

# Weekend exclusion
schedule:
  every: 4h
  except: [saturday, sunday]
```

**Restart safety:** `last_dispatch` is persisted before running. Tasks dispatched within 2h are considered "in-flight" and won't be re-dispatched after restart.

## Scheduler Task Priority

Tasks sort by `priority` first (lower = runs first), with overdue score as tiebreaker. This ensures pipeline ordering — upstream stages run before downstream when multiple tasks are overdue.

Tasks with default priority (99) sort after all prioritized tasks. Fillers always run after all regular tasks regardless of priority.

## Workflow-Backed Tasks

A scheduler task can dispatch a pi workflow DAG (Phase 3) instead of shelling out
to `agentwire ensure`. Use `workflow:` + `inputs:` on the task; omit `session:` and
`task:`. Workflow tasks run in-process via `agentwire.workflows.runner.run_workflow`,
bypass tmux entirely, and write their run under `~/.agentwire/workflows/runs/<run_id>/`.

```yaml
tasks:
  nightly-doc-audit:
    schedule: { every: day, at: "23:00" }
    workflow: doc-drift-check        # workflow name or absolute YAML path
    inputs:
      paths: "docs/,agentwire/"
      context: "{{ project }}"       # {{ task }}, {{ project }}, {{ session }}, {{ workflow }} expand from scheduler context
    gate:
      git_diff: [docs/, agentwire/]  # git gates still need `project:`
    project: /Users/dotdev/projects/agentwire-dev
    max_runs: 30
```

**Status mapping:** workflow `success` → `complete`, `partial` → `incomplete`,
`failure` → `failed`. Node-level detail is included in the `task_completed`
event (`workflow`, `run_id`, `nodes[]`) and rendered in `agentwire scheduler
report --artifact`.

**Dry-run:** `agentwire scheduler run <name> --dry-run` prints the workflow
plan without touching state — workflow tasks only.

**Rule:** a task must set either `task:` (ensure path) or `workflow:` (DAG path),
not both. `inputs:` is only valid with `workflow:`. Git gates (`git_commit`,
`git_diff`) require `project:` to be set regardless of dispatch path.

## One-Time and Limited Tasks

Tasks can auto-disable after a set number of runs:

```yaml
tasks:
  tonight-scaffold:
    # ...
    once: true        # Run once, then auto-disable (shorthand for max_runs: 1)
    schedule:
      every: 1m       # Run ASAP

  quarterly-report:
    # ...
    max_runs: 4       # Run 4 times then auto-disable
    schedule:
      every: day
      at: "09:00"
```

- `once: true` — shorthand for `max_runs: 1`
- `max_runs: N` — auto-disables task after N successful dispatches
- Scheduler logs a `task_disabled` event with `reason: max_runs_reached`
- Re-enabling a disabled task via `agentwire scheduler enable <name>` resets it

## Overnight Session System

"Prepare once, fork many, execute overnight." Human prepares sessions interactively during the day (full back-and-forth with Claude), queues them, and the orchestrator dispatches them during off-hours with draft PR creation.

**User workflow:**
```
5:00 PM — Open session, discuss project context with Claude
5:15 PM — agentwire overnight prepare --from piinpoint --task "refactor payment module"
           → Queued. Session context captured.
5:16 PM — Repeat for more tasks
5:43 PM — Go home.

10:00 PM — Orchestrator dispatches session 1 → works → PR created
11:00 PM — Dispatches session 2 → works → PR created
12:00 AM — All done. Voice notification sent.

8:00 AM — agentwire overnight report → review draft PRs
```

**Queue directory:** `~/.agentwire/overnight/` (active items), `~/.agentwire/overnight/done/` (archived)

**How it works:**
1. `prepare` captures: Claude sessionId (for `--resume --fork-session`), git branch, HEAD commit
2. `dispatch` creates tmux session, launches agent with forked context, creates work branch
3. On completion: auto-commit, push, draft PR, archive, notify
4. Orchestrator respects work window (default 22:00-07:00)

**Session type:** Uses `claude-auto` by default for classifier safety net. Override with `--type`.
