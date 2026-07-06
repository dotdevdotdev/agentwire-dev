---
name: agentwire-scheduler
description: Scheduler configuration — task gates (`git_commit`, `git_diff`, `command`), `schedule` field reference (duration vs calendar, `at`/`every`/`after`/`delay`/`cooldown`/`not_before`/`not_after`/`except`), priority/pipeline ordering, one-time/max_runs tasks. Use when adding or debugging scheduled tasks in `~/.agentwire/scheduler.yaml`, or explaining how gating/dispatch works.
---

# AgentWire Scheduler

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

## Worktree + PR Mode (the standard for repo work)

Tasks that touch a git repo run in an **isolated worktree** forked off the latest `base` branch, and their output is proposed back as a **draft PR** — the live project folder is never modified and nothing lands on `main` unreviewed.

```yaml
tasks:
  doc-drift:
    project: ~/projects/agentwire-dev
    worktree: true       # DEFAULT when project is a git repo
    base: main           # fork the worktree off latest origin/main
    pr_target: main      # draft PR base branch (defaults to `base`)
    pr_draft: true       # open as draft (default)
  ai-morning-briefing:
    project: ~/projects/agentwire-dev
    worktree: false      # research/email task — runs in place, never commits
```

| Field | Default | Meaning |
|-------|---------|---------|
| `worktree` | auto (`true` if `project` is a git repo) | Run isolated + open a PR. Set `false` for tasks that only email/produce side effects. |
| `base` | `main` | Branch the worktree forks from (fetched fresh from `origin`). |
| `pr_target` | `base` | PR base branch. |
| `pr_draft` | `true` | Open the PR as a draft. |

**Lifecycle:** branch `scheduler-<task>-<timestamp>` → run in worktree → if it produced changes, `git add -A` + commit (`scheduler: <task> — <summary>`) + push + draft PR; if it produced **nothing**, the empty worktree is removed and no PR is opened. The worktree **persists while the PR is open** (so you can spawn a session there during review) and is **reaped automatically when the PR merges or closes** (worktree + branch + session torn down). `worktree: false` tasks run in place and never commit.

**Seeded files:** `git worktree add` only checks out *tracked* files, so gitignored secrets/config (`.env`, `.agentwire.yml`, `.agentwire.tasks.yml`) won't be in a fresh worktree — an agent that needs them to authenticate or find its task definitions would fail. The files listed in `projects.worktrees.copy_files` (default `[".env", ".agentwire.yml", ".agentwire.tasks.yml"]`) are copied from the main repo into every new worktree. They stay gitignored there, so they're never committed.

**Keep `.agentwire.yml`/`.agentwire.tasks.yml` gitignored, not tracked.** Worktree runs check out HEAD — if the project *tracks* either, uncommitted live edits are silently invisible to the run, which executes the stale committed version (and `copy_files` won't overwrite a tracked checkout). Gitignored + seeded via `copy_files` means the live file always wins. See the `agentwire-project-config` skill.

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
