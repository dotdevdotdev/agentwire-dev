# AgentWire Feature Requests for PiinPoint Async Agents

> These features would make the PiinPoint overnight agent workflow config-only
> (no custom tooling needed). Ordered by priority for the use case.

---

## Context: What We're Trying To Do

Run overnight autonomous Claude Code agents against PiinPoint repos. Each task
should fork from a specified branch/commit, do its work on a new branch, and
open a PR back to the source. Tasks should be independent (no shared state)
with self-healing capabilities. A morning dashboard summarizes all overnight work.

**Goal:** pure config in `.agentwire.yml` + `scheduler.yaml` — no custom scripts.

---

## Feature 1: Branch Management in Task Config (HIGH PRIORITY)

### What exists today
- Tasks have `prompt`, `pre`, `post`, `on_task_end`, retry config, loop config
- No concept of starting branch, target branch, or auto branch creation
- Branch management would have to be hacked into pre/post commands manually

### What we need
New optional fields in task config:

```yaml
tasks:
  write-tests:
    prompt: "Write missing unit tests for recent changes"

    # New fields:
    starting_ref: main              # Any valid git ref (branch, SHA, tag)
                                    # Default: repo's default branch
    work_branch: agent/write-tests  # Branch name for the agent's work
                                    # Default: agent/<task-name>-<YYYY-MM-DD>
    pr_target: main                 # Branch to PR against when done
                                    # Default: starting_ref value
    pr_draft: true                  # Create as draft PR (default: true)
```

### How it would work in the task lifecycle

Between step 2 (ensure session healthy) and step 4 (run pre-commands):
```
1. Resolve starting_ref to a commit SHA
2. git checkout <starting_ref> (ensure we're on the right base)
3. git pull (get latest if it's a branch)
4. git checkout -b <work_branch> (create the agent's working branch)
```

After step 9 (post-commands), new auto step:
```
1. git add -A && git commit (if uncommitted changes)
2. git push -u origin <work_branch>
3. Create PR: work_branch -> pr_target (via gh CLI or API)
4. git checkout <starting_ref> (reset for next task)
```

### Why not just use pre/post commands?
- Every task would repeat the same boilerplate
- Easy to get wrong (forget to reset, wrong branch, etc.)
- The lifecycle should handle branch plumbing so prompts stay focused on work
- Makes the task config self-documenting — you can see what branch a task targets

### Edge cases to handle
- `starting_ref` doesn't exist: fail task with clear error in summary
- Work branch already exists: append timestamp or increment suffix
- No changes to commit: skip PR creation, note in summary
- PR creation fails: log error but don't fail the task (changes are still pushed)

---

## Feature 2: Commit Pinning in Session Fork (MEDIUM PRIORITY)

### What exists today
- `session_fork` creates a new session + worktree from HEAD of source branch
- No way to specify a commit to fork from

### What we need
Optional `--commit` / `commit` parameter on fork:

```bash
agentwire fork source-session target-session/branch --commit abc123
```

```python
# MCP tool
session_fork(session="project", target="project/feature", commit="abc123")
```

### Why
When forking multiple parallel tasks from the same parent, we need them all to
start from the exact same commit, not from HEAD which may have moved. This is
the parallel execution case — sequential tasks can use branch management (Feature 1)
instead.

### Implementation notes
- After `git worktree add`, do `git checkout <commit>` in the new worktree
- Or use `git worktree add -b <branch> <path> <commit>` (git supports this natively)

---

## Feature 3: One-Time Task Queue (MEDIUM PRIORITY)

### What exists today
- Scheduler runs tasks on recurring intervals (`every: "2h"`, `every: "day"`, etc.)
- Dependency-triggered tasks (`after: other_task`) but still recurring
- No concept of "run this once tonight and then stop"

### What we need
A way to enqueue one-time tasks that run once and are removed (or marked done).

**Option A: `once: true` flag in scheduler.yaml**
```yaml
tasks:
  tonight-feature-scaffold:
    project: ~/projects/piinpoint
    session: piinpoint
    task: scaffold-payments
    once: true                    # Run once, then auto-disable
    schedule:
      every: "1m"                 # Run ASAP (first eligible moment)
```

**Option B: Dedicated queue file or CLI command**
```bash
# CLI to enqueue a one-time task
agentwire scheduler queue --session piinpoint --task write-tests

# Or a queue section in scheduler.yaml
queue:
  - session: piinpoint
    task: write-tests
  - session: piinpoint
    task: lint-cleanup
```

**Option C: `run_count` limit**
```yaml
tasks:
  tonight-tests:
    # ...
    max_runs: 1                   # Auto-disable after N runs
```

### Why
The nightly workflow is: "before I leave, queue up 3-5 tasks for tonight."
These aren't recurring — they're specific to tonight's work. Making them
recurring with manual disable/enable is clunky.

### Preference
Option C (`max_runs`) is the most flexible — it handles one-time AND
"run this 3 times then stop" cases. Option B (dedicated queue) is the
most intuitive UX for the "load up tonight's work" flow. Could do both.

---

## Feature 4: Starting Session / Context Inheritance (MEDIUM PRIORITY)

### What exists today
- Tasks specify a `session` to run in
- Sessions can be pre-loaded with context (CLAUDE.md, conversation history)
- But there's no way for a task to say "start from this session's context"
  while running in a NEW session (to avoid polluting the source)

### What we need
Optional `starting_session` in task config:

```yaml
tasks:
  continue-feature:
    prompt: "Continue the payments refactor"
    starting_session: payments-loaded    # Fork context from this session
    starting_ref: feature/payments
```

### How it would work
1. If `starting_session` is set and differs from the task's target session:
   - Fork the starting_session into a new session (or the task's session)
   - This carries over Claude's conversation context / loaded knowledge
2. If not set: create fresh session (current behavior)

### Why
Some tasks benefit from a pre-loaded session where you've already explained
the codebase, the ticket, the approach. The agent shouldn't have to rediscover
all that context. But you also don't want the overnight work to pollute the
source session — so fork it.

### This may already be close
`session_fork` exists and copies Claude session files. The gap is wiring this
into the task lifecycle so it happens automatically based on config.

---

## Feature 5: Morning Dashboard Task (LOW PRIORITY - NICE TO HAVE)

### What exists today
- `desktop_write_artifact` can generate HTML in the portal
- `scheduler_history` and `scheduler_events` provide run data
- But there's no built-in "generate a summary of all overnight work" mechanism

### What we need
A built-in or easily-configurable "morning report" that:
1. Runs after all overnight tasks complete (use `after:` dependencies)
2. Collects: task names, statuses, durations, summaries, branches created, PRs opened
3. Generates an HTML artifact dashboard
4. Optionally posts to Slack or sends notification

### Implementation ideas

**Option A: Built-in scheduler report command**
```bash
agentwire scheduler report --since "8 hours ago" --format html --artifact
```

**Option B: A standard role/task template**
Ship a bundled `morning-report` task that users can add to their scheduler:
```yaml
tasks:
  morning-report:
    project: ~/projects/piinpoint
    session: piinpoint-report
    task: morning-report          # Bundled task template
    schedule:
      after: [write-tests, lint-cleanup, dep-updates]
      delay: "5m"
```

**Option C: Post-scheduler hook**
A hook that runs after all tasks in a cycle complete, auto-generates the report.

### Why
The morning dashboard is critical for the human-in-the-loop workflow. Without
it, you're manually checking each session, each branch, each PR. The dashboard
is what makes overnight agents practical for a team.

---

## Feature 6: Per-Task Role Override (LOW PRIORITY)

### What exists today
- Roles are set at session creation time
- Scheduler entries can override `roles:` per task — but this applies to the session
- Tasks in `.agentwire.yml` don't have a `role:` field

### What we need
```yaml
# In .agentwire.yml
tasks:
  write-tests:
    prompt: "Write tests"
    role: piinpoint-test-writer    # Use this role for this task

  lint-cleanup:
    prompt: "Fix lint"
    role: task-runner              # Different role for this task
```

### Why
Different overnight tasks benefit from different personas. A test writer needs
different instructions than a PR reviewer or a lint fixer. Currently you'd need
separate sessions with different roles, or one session that tries to be everything.

### Note
The scheduler.yaml already has `roles:` per entry, so this may partially work
already at the scheduler level. The gap is having it in `.agentwire.yml` task
definitions for non-scheduled (queue-based) usage.

---

## Summary: Priority Order

| # | Feature | Priority | Effort Est. | Impact |
|---|---------|----------|-------------|--------|
| 1 | Branch management in task config | HIGH | Medium | Eliminates all custom scripting for branch workflow |
| 2 | Commit pinning in session fork | MEDIUM | Small | Enables reliable parallel execution |
| 3 | One-time task queue | MEDIUM | Medium | Enables "queue up tonight's work" flow |
| 4 | Starting session / context inheritance | MEDIUM | Medium | Pre-loaded context for complex tasks |
| 5 | Morning dashboard | LOW | Medium | Team-facing reporting (can be hacked with post-commands for now) |
| 6 | Per-task role override | LOW | Small | Already partially available via scheduler.yaml |

**With just Feature 1 (branch management), the overnight workflow becomes
config-only.** The rest are quality-of-life improvements that make it smoother.

---

## What Already Works (No Changes Needed)

These existing features cover our needs without modification:

- **Task lifecycle** (lock, pre, prompt, idle-wait, summary, post) — exactly right
- **Pre-command templating** (`{{ var_name }}`) — useful for injecting dynamic context
- **Retry config** (retries, retry_delay) — handles transient failures
- **Scheduler gates** (git_commit, git_diff, command) — smart skip for no-op runs
- **Scheduler dependencies** (`after:`, `delay:`) — chain tasks when needed
- **Time windows** (not_before, not_after, except days) — restrict overnight hours
- **Output capture & notify** — save summaries, send alerts
- **Roles** (project, user, bundled discovery) — composable agent personas
- **Task-runner role** — already optimized for headless autonomous work
- **Session management** — create, fork, kill, monitor
- **Artifact system** — HTML dashboards in the portal
- **Remote machines** — run on always-on devboxes
