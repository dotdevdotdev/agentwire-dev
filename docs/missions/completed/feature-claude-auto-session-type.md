# Feature Request: Auto Mode Session Type (`claude-auto`)

> Priority: HIGH — direct safety upgrade for all autonomous/overnight agent workflows.
> Depends on: Claude Code auto mode (shipped March 24, 2026).

---

## Summary

Add a new session type `claude-auto` that uses Claude Code's auto mode instead of
`bypassPermissions`. This gives agents the same autonomous execution capability as
`claude-bypass` but with an AI classifier that blocks dangerous actions. It's a
strict safety upgrade for any task running unattended.

---

## Background: What is Claude Code Auto Mode?

Auto mode (launched March 24, 2026) is a permission mode where a background classifier
model (Sonnet 4.6) reviews each tool call before execution:

- **Safe actions** (file reads, edits, git operations): execute immediately, no prompt
- **Risky actions** (mass deletes, credential exfil, force push to main): blocked,
  Claude attempts alternative approaches
- **Fallback**: after 3 consecutive blocks or 20 total, falls back to manual prompts

### How it compares to what we use today

| | `claude-bypass` (current) | `claude-auto` (proposed) |
|-|--------------------------|-------------------------|
| Permission prompts | None | None (classifier decides) |
| Safety checks | **NONE** | AI classifier blocks dangerous actions |
| Mass file deletion | Allowed | **Blocked** |
| Credential exfiltration | Allowed | **Blocked** |
| Force push to main | Allowed | **Blocked** |
| Normal file edits | Allowed | Allowed (auto-approved, no classifier cost) |
| Git branch/commit/push | Allowed | Allowed (auto-approved) |
| Bash commands | Allowed | Allowed if safe (classifier reviews) |
| Token overhead | None | ~20% more for command-heavy tasks |
| Fallback on block | N/A | Falls back to prompt (AgentWire can detect) |

**Bottom line**: `claude-auto` does everything `claude-bypass` does for normal overnight
work, but prevents the catastrophic failures that could happen when an agent goes off
the rails at 3am with nobody watching.

---

## What We Want

### 1. New session type: `claude-auto`

When creating a session with `type: claude-auto`, AgentWire should launch Claude Code
with auto mode enabled instead of `--dangerously-skip-permissions`.

**CLI mapping:**
```
claude-bypass  →  claude --dangerously-skip-permissions
claude-auto    →  claude --enable-auto-mode --permission-mode auto
```

**Usage:**
```bash
# CLI
agentwire new myproject --type claude-auto

# MCP
session_create(name="myproject", session_type="claude-auto")
```

### 2. Support in task/scheduler config

```yaml
# .agentwire.yml — per-project default
session_type: claude-auto

# scheduler.yaml — per-task override
tasks:
  nightly-tests:
    project: ~/projects/piinpoint
    session: piinpoint-tests
    task: write-tests
    type: claude-auto              # Already supported field, just new value
```

### 3. Support in session fork

```bash
agentwire fork myproject myproject/feature --type claude-auto
```

### 4. Make it the default for task-runner role (optional, discuss)

Since `task-runner` is the role designed for headless autonomous execution,
`claude-auto` is arguably the right default session type for it. Currently
the docs and examples use `claude-bypass`. Auto mode is strictly safer.

Could be a role-level default:
```markdown
---
name: task-runner
description: Optimized for scheduled/headless task execution
session_type: claude-auto
---
```

Or just update the documentation/examples to recommend `claude-auto` over
`claude-bypass` for production repos.

---

## Detailed Behavior

### How Claude Code auto mode is enabled

The key CLI flags:
```bash
claude --enable-auto-mode --permission-mode auto [rest of args]
```

Both flags are needed:
- `--enable-auto-mode` makes auto mode available
- `--permission-mode auto` activates it

Without `--enable-auto-mode`, you can't cycle to auto mode via Shift+Tab either.

### What happens to allow rules

**Important**: when auto mode activates, Claude Code **strips broad allow rules**:
- `Bash(*)` — blanket shell access removed
- `Bash(python*)`, `Bash(node*)` — wildcard interpreters removed
- Package-manager run commands removed
- `Agent` allow rules removed

**Specific allow rules survive**: `Bash(git *)`, `Bash(npm test)`, `Bash(make build)`, etc.

**Implication for AgentWire**: if `claude-bypass` sessions currently rely on blanket
`Bash(*)` permission (likely, since bypass skips all checks), `claude-auto` sessions
need explicit specific allow rules instead. This could be:

a) **Configured per-project** in `.claude/settings.json`:
```json
{
  "permissions": {
    "allow": [
      "Bash(git *)",
      "Bash(npm *)",
      "Bash(npx *)",
      "Bash(make *)",
      "Read(*)",
      "Edit(*)",
      "Write(*)"
    ]
  }
}
```

b) **Injected by AgentWire** when launching a `claude-auto` session based on task needs.
This is a more advanced integration but would be cleaner.

c) **Documented as a setup step** — "before using `claude-auto`, configure your project's
allow rules in `.claude/settings.json`."

**Recommendation**: start with (c) for simplicity. Users configure their project-level
allow rules once. AgentWire just launches with the right flags.

### Classifier fallback behavior

When the classifier blocks an action 3 times consecutively (or 20 times total in a
session), auto mode falls back to manual prompting. In a headless session, this
effectively means the agent stalls — it's waiting for input that won't come.

**AgentWire should detect this.** Possible approaches:
- Monitor session output for permission prompt patterns
- If detected, log it as a task failure with `status: blocked`
- Include the blocked action in the summary for morning review
- Optionally: auto-kill the session after a timeout

This is similar to how `claude-bypass` sessions handle other stall conditions.
The idle timeout in the task config (`idle_timeout: 30`) may already cover this —
if the agent is stalled waiting for permission, it's "idle" from AgentWire's perspective.

### Token cost implications

Auto mode adds ~20% token overhead for command-heavy tasks due to classifier calls:
- File reads/edits: **no extra cost** (auto-approved without classifier)
- Each bash command: classifier reviews the command (costs tokens)
- Network requests: classifier reviews (costs tokens)

For overnight agents, this means:
- A task that's mostly file edits (test writing, refactoring): minimal overhead
- A task that runs many bash commands (dep updates, build/test cycles): ~20% more
- Still well within the idle quota budget

---

## Allow Rule Layers: AgentWire Core + User + Project

Auto mode strips broad `Bash(*)` rules. This means AgentWire and its users need
explicit allow rules at multiple layers. Claude Code uses two settings files:

- **User-level**: `~/.claude/settings.json` — applies to all projects
- **Project-level**: `<project>/.claude/settings.json` — applies to one project

AgentWire needs to ensure its own operational commands are always allowed, while
letting users add project-specific and user-specific rules on top.

### Layer 1: AgentWire Core Allows (injected by AgentWire)

**Note:** AgentWire's own orchestration (tmux, branch management pre/post steps,
session lifecycle) runs as subprocess calls OUTSIDE the Claude Code session — those
don't need allow rules. These core allows are for commands the Claude Code agent
inside the session might reasonably need during any task:

AgentWire should **inject these into the Claude Code launch args** (via `--allowedTools`)
or ensure they're in the user-level settings when a `claude-auto` session is created:

```json
{
  "permissions": {
    "allow": [
      "Bash(agentwire *)",
      "Bash(tmux *)",
      "Bash(git status*)",
      "Bash(git rev-parse*)",
      "Bash(git checkout*)",
      "Bash(git branch*)",
      "Bash(git add*)",
      "Bash(git commit*)",
      "Bash(git push*)",
      "Bash(git pull*)",
      "Bash(git log*)",
      "Bash(git diff*)",
      "Bash(git worktree*)",
      "Bash(gh pr create*)",
      "Bash(gh pr view*)",
      "Read(*)",
      "Edit(*)",
      "Write(*)",
      "Glob(*)",
      "Grep(*)"
    ]
  }
}
```

**These should NOT require user configuration.** When AgentWire launches a
`claude-auto` session, it should ensure these rules are present automatically.

**Implementation options:**
- (a) Pass via `--allowedTools` flag at launch (cleanest, no file mutation)
- (b) Write to a temporary settings overlay that merges with user settings
- (c) Document as required setup (least work, but error-prone)

**Recommendation:** Option (a) — pass core allows via CLI args. This keeps them
separate from user-managed settings and avoids file mutation.

### Layer 2: User-Level Allows (`~/.claude/settings.json`)

User-configured rules that apply across all projects. These are the user's
responsibility to configure once. They persist across all projects and sessions.

**Already deployed to all our machines:**
```json
{
  "permissions": {
    "allow": [
      "Bash(git *)",
      "Bash(gh *)",
      "Bash(ls *)",
      "Bash(which *)",
      "Bash(cat *)",
      "Bash(wc *)",
      "Bash(sort *)",
      "Bash(head *)",
      "Bash(tail *)",
      "Bash(mkdir *)",
      "Bash(cp *)",
      "Bash(mv *)",
      "Bash(chmod *)",
      "Bash(find *)",
      "Bash(grep *)",
      "Bash(diff *)",
      "Bash(pwd)",
      "Bash(date *)"
    ],
    "deny": [
      "Bash(rm -rf /)*",
      "Bash(rm -rf ~)*",
      "Bash(env *)",
      "Bash(printenv*)",
      "Bash(export *)"
    ]
  }
}
```

**Design decisions:**
- No `rm` in allow list — classifier handles individual file deletes case by case
- Explicit deny on `rm -rf /` and `rm -rf ~` — catastrophic mass delete prevention
- Explicit deny on `env`, `printenv`, `export` — no secrets/environment access
- No `curl`/`wget` — classifier reviews network access case by case
- No `npm`/`node`/`python` — these go in per-project settings (Layer 3)

### Layer 3: Project-Level Allows (`<project>/.claude/settings.json`)

Project-specific rules for the tools/commands that project needs:

```json
// ~/projects/piinpoint/.claude/settings.json
{
  "permissions": {
    "allow": [
      "Bash(npm test*)",
      "Bash(npm run lint*)",
      "Bash(npm run build*)",
      "Bash(npx jest*)",
      "Bash(npx eslint*)",
      "Bash(npx tsc*)",
      "Bash(npx prisma*)"
    ]
  }
}
```

### How the layers merge

Claude Code merges settings: project overrides user, both apply together for allows.
The effective allow list for a `claude-auto` session would be:

```
AgentWire core allows (injected via --allowedTools)
  + User-level allows (~/.claude/settings.json)
  + Project-level allows (<project>/.claude/settings.json)
  = Full allow list for the session
```

Allow rules bypass the classifier entirely — pre-approved commands execute immediately
with zero token cost. Everything NOT in the allow list goes through the classifier.

### Per-Task Allows (future enhancement, optional)

Some tasks need specific commands that others don't. Could add an `allowed_tools`
field to task config:

```yaml
tasks:
  run-migrations:
    prompt: "Run database migrations"
    starting_ref: main
    allowed_tools:                    # Additional allows for this task only
      - "Bash(npx prisma migrate*)"
      - "Bash(psql *)"
```

These would be appended to the `--allowedTools` flag at session launch.
Not critical for Phase 1, but a natural extension.

---

## Expected Configuration: PiinPoint Overnight Workflow

### Project config (`.agentwire.yml`)
```yaml
session_type: claude-auto

tasks:
  write-tests:
    prompt: "Write missing unit tests for recent changes in the payments module"
    starting_ref: main
    pr_draft: true
    role: piinpoint-test-writer
    retries: 1
    idle_timeout: 60
    exit_on_complete: true

  lint-cleanup:
    prompt: "Run the linter, fix all auto-fixable issues, commit the fixes"
    starting_ref: main
    role: task-runner
    exit_on_complete: true

  dep-updates:
    prompt: |
      Check for outdated dependencies. For each one:
      1. Create a branch
      2. Bump the version
      3. Run the test suite
      4. Only commit if tests pass
      Summarize which updates passed and which failed.
    starting_ref: main
    role: task-runner
    exit_on_complete: true
```

### Scheduler config (`~/.agentwire/scheduler.yaml`)
```yaml
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
    post:
      - "agentwire scheduler report --since 12h --artifact"
```

### Project allow rules (`~/projects/piinpoint/.claude/settings.json`)

Only project-specific commands needed here — AgentWire core allows (git, gh, read,
edit, etc.) are injected automatically via `--allowedTools` at launch.

```json
{
  "permissions": {
    "allow": [
      "Bash(npm test*)",
      "Bash(npm run lint*)",
      "Bash(npm run build*)",
      "Bash(npx jest*)",
      "Bash(npx eslint*)",
      "Bash(npx tsc*)"
    ]
  }
}
```

---

## Migration Path

### Phase 1: Add `claude-auto` session type
- Map `claude-auto` to `--enable-auto-mode --permission-mode auto`
- Auto-inject AgentWire core allow rules via `--allowedTools` at session launch
  (git, gh, agentwire, tmux, Read, Edit, Write, Glob, Grep)
- No changes to existing `claude-bypass` behavior
- Users opt in per-task or per-project
- Users add project-specific allows in `<project>/.claude/settings.json`

### Phase 2: Update documentation and examples
- Recommend `claude-auto` over `claude-bypass` for production repos
- Document the three-layer allow model (AgentWire core + user + project)
- Update task-runner role docs

### Phase 3: Consider making `claude-auto` the default
- Once battle-tested, could become the default for task-runner sessions
- `claude-bypass` remains available for sandboxed/isolated environments
- Could be a config flag: `default_session_type: claude-auto` in `~/.agentwire/config.yaml`

---

## Edge Cases

### Agent hits classifier fallback (3+ blocks)
- AgentWire's idle timeout should catch this (agent stalls waiting for prompt)
- Task reports as `timeout` or `blocked` in scheduler history
- Morning dashboard shows which tasks stalled and why
- Human reviews in the morning and adjusts allow rules if needed

### Allow rules too restrictive
- If the classifier + allow rules block too many actions, the agent can't do its work
- Start with broad-ish allows (`Bash(git *)`, `Bash(npm *)`) and tighten over time
- Monitor scheduler events for blocked/timeout statuses

### Allow rules too broad
- Auto mode strips `Bash(*)` automatically — can't accidentally bypass everything
- Specific allows like `Bash(npm *)` are fine — classifier still reviews the actual command
- The classifier is the safety net even when allow rules are generous

### Mixed session types in parallel
- Some tasks use `claude-auto` (production repos)
- Some tasks use `claude-bypass` (sandboxed experiments)
- Both can run simultaneously — session type is per-session, not global

### Haiku model not supported
- Auto mode requires Sonnet 4.6 or Opus 4.6
- Tasks using `model: haiku` can't use `claude-auto`
- AgentWire should warn or fall back to `claude-bypass` if model is incompatible
