> Living document. Update this, don't create new versions.

# Scheduled Workloads

Reliable headless task execution for overnight and automated agent workflows.

---

## Overview

Three paths for running scheduled work, picked per task:

1. **`agentwire ensure` tasks** — Reliable session management + task execution with lifecycle hooks. A full Claude Code session runs a single prompt from `.agentwire.yml`. Best for multi-step agent work that needs its own branch / PR / MCP tools.
2. **Pi workflow tasks** — A YAML DAG of `pi -p --mode json` invocations runs in-process. No tmux session, no project required. Best for deterministic pipelines where each step has clear inputs/outputs and you want cheap per-node retries.
3. **Tasks in `.agentwire.yml`** — Named tasks with pre/prompt/post phases and branch management — the substrate for path #1.

All three are orchestrated by `~/.agentwire/scheduler.yaml` and the AgentWire scheduler daemon. A single board can mix ensure tasks and workflow tasks freely.

---

## Choosing ensure vs workflow

| | ensure task | workflow task |
|---|---|---|
| Schedule field | `task: <name>` + `session:` | `workflow: <name-or-path>` + `inputs:` |
| Dispatch | `agentwire ensure` subprocess → tmux session → Claude Code | `run_workflow()` in-process → pi subprocesses per node |
| Model family | Claude (subscription via session type) | Z.AI glm-5.1 by default (any pi-supported model per node) |
| Session needed | yes | no |
| Project needed | yes (for branch mgmt) | optional (only for git gates / auto-commit) |
| Multi-step logic | inside one Claude prompt | first-class DAG with retries, branches, outputs |
| Dry-run | no | `scheduler run <name> --dry-run` prints the plan |
| Cost profile | subscription-covered | Z.AI credits (or free flash tier) |

A task must set exactly one of `task:` or `workflow:`. Everything else (gates, priority, `max_runs`, `once`, `cooldown`, `not_before`/`not_after`) applies to both.

Full workflow task reference: [`workflows.md#scheduler-integration`](workflows.md#scheduler-integration).

---

## Task Definition Schema

Define tasks in `.agentwire.yml`:

```yaml
type: claude-auto    # Recommended for overnight work — see claude-auto below
roles:
  - task-runner
shell: /bin/sh       # Default shell for task commands

tasks:
  write-tests:
    # Execution control
    shell: /bin/bash         # Override shell for this task
    priority: 10             # Pipeline ordering (lower = higher priority, default: 99)
    retries: 2               # Retry on failure (default: 0)
    retry_delay: 30          # Seconds between retries (default: 30)
    idle_timeout: 60         # Seconds of idle before completion (default: 30)
    exit_on_complete: true   # Exit session after completion (default: true)
    role: piinpoint-test-writer  # Role override for this task (optional)

    # Branch management (for autonomous overnight workflows)
    starting_ref: main       # Git ref to checkout before task runs
    work_branch: agent/task  # Branch for agent's work (default: agent/<task>-<date>)
    pr_target: main          # PR target branch (default: starting_ref)
    pr_draft: true           # Create as draft PR (default: true)

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
      notify: voice                  # voice | alert | webhook ${URL} | command "..."
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

---

## `ensure` Command

```bash
agentwire ensure -s session --task name              # Run named task
agentwire ensure -s session --task name --timeout 600   # Custom timeout
agentwire ensure -s session --task name --wait-lock     # Wait if locked
agentwire ensure -s session --task name --dry-run       # Preview without executing
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

Schedule tasks in `~/.agentwire/scheduler.yaml`:

```yaml
tasks:
  nightly-tests:
    project: ~/projects/piinpoint
    session: piinpoint-tests
    task: write-tests
    type: claude-auto          # Session type override
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
    type: claude-auto
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

After overnight tasks run, generate a summary report:

```bash
agentwire scheduler report --since 8h           # Print summary + artifact path
agentwire scheduler report --since 8h --artifact  # Also open in portal
```

The HTML report includes: task name, status badge, branch, PR link, duration, and one-line summary. PR URLs are populated automatically when tasks use `starting_ref` + `work_branch`.

---

## `claude-auto` — Recommended Session Type

For overnight/unattended work, use `claude-auto` instead of `claude-bypass`:

```yaml
# .agentwire.yml
type: claude-auto
```

`claude-auto` uses Claude Code's auto mode: a background Sonnet 4.6 classifier reviews each tool call before execution. Safe actions (file reads, edits, git ops) run immediately with no overhead. Dangerous actions (force push to main, mass deletion, credential exfiltration) are blocked.

`claude-bypass` has no safety checks. `claude-auto` does everything `claude-bypass` does for normal overnight work but prevents catastrophic failures at 3am when nobody's watching.

**Requires:** Team or Enterprise Claude plan. Pro/Max individual plans not supported.

See `../sessions/claude-code-auto-mode.md` for full setup, allow rule configuration, and constraints.

---

## Full Overnight Workflow Example

```yaml
# ~/projects/piinpoint/.agentwire.yml
type: claude-auto
roles:
  - task-runner

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
    prompt: "Summarize what was accomplished overnight. Check the PRs that were opened."
    post:
      - "agentwire scheduler report --since 12h --artifact"
    exit_on_complete: true
```

---

## Overnight Session Queue

For tasks that need human-in-the-loop preparation, use the **overnight session queue** instead of the scheduler. The scheduler runs predefined YAML tasks. The overnight queue dispatches dynamically prepared sessions with forked Claude conversation context.

**Key difference:** Autonomous agents fail on judgment-heavy work because they lack micro-decisions humans make. The overnight system front-loads all human judgment into interactive preparation time, then dispatches the fully-prepared sessions overnight.

### Workflow

```
5:00 PM — Open session, discuss project context with Claude
5:15 PM — agentwire overnight prepare --from piinpoint --task "refactor payment module"
           → Session context captured (Claude sessionId, git branch, HEAD commit)
5:16 PM — Keep preparing more tasks from same or different sessions
5:43 PM — Go home.

10:00 PM — Orchestrator dispatches session 1 (forks Claude context) → works → PR
11:00 PM — Dispatches session 2 → works → PR
12:00 AM — All done. Voice notification sent.

8:00 AM — agentwire overnight report → review draft PRs
```

### Commands

```bash
agentwire overnight prepare --from <session> --task "description"  # Queue session
agentwire overnight list [--all]            # List queue (--all includes done/)
agentwire overnight status                  # Orchestrator state + queue summary
agentwire overnight cancel <id>             # Cancel queued/running item
agentwire overnight priority <id> <n>       # Update priority (lower = higher)
agentwire overnight start                   # Start orchestrator in tmux
agentwire overnight serve                   # Run orchestrator in foreground
agentwire overnight stop                    # Stop orchestrator
agentwire overnight report                  # Morning report of completed items
```

### How Dispatch Works

1. `prepare` captures: Claude sessionId (for `--resume --fork-session`), git branch, HEAD commit
2. Orchestrator creates tmux session, launches agent with forked conversation context
3. Agent checks out work branch (`overnight/<id>-<slug>`)
4. Go prompt sent — agent executes with full prior context
5. On completion: auto-commit, push, draft PR, archive to `done/`, notify

### Configuration

Add to `~/.agentwire/config.yaml`:

```yaml
overnight:
  window_start: "22:00"       # Dispatch window start
  window_end: "07:00"         # Dispatch window end
  timezone: "America/Toronto" # Empty = local
  check_interval: 60          # Seconds between queue checks
  max_concurrent: 1           # Sessions to run at once
  session_timeout: 7200       # Max seconds per session (2h)
  session_type: "claude-auto" # Default session type
  pr_draft: true              # Create draft PRs
```

### When to Use What

| Workflow | Tool | Best For |
|----------|------|----------|
| Predefined recurring tasks | **Scheduler** | Nightly tests, lint, reports |
| Human-prepared one-shot work | **Overnight queue** | Feature work, complex refactors |
| Quick one-off tasks | **`agentwire ensure`** | Ad-hoc task execution |

```yaml
# ~/.agentwire/scheduler.yaml
tasks:
  nightly-tests:
    project: ~/projects/piinpoint
    session: piinpoint-tests
    task: write-tests
    type: claude-auto
    once: true
    schedule:
      every: 1m
      not_before: "22:00"
      not_after: "06:00"

  nightly-lint:
    project: ~/projects/piinpoint
    session: piinpoint-lint
    task: lint-cleanup
    type: claude-auto
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
