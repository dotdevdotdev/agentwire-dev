> Living document. Update this, don't create new versions.

# Mission: Per-Task Role Override

## Goal

Tasks can declare `role: <rolename>` to run with a specific role, overriding the
session's default roles. The role is applied when the session is created/recreated
for the task.

## Status: Complete

## Use Case

```yaml
# In .agentwire.yml
tasks:
  write-tests:
    prompt: "Write unit tests for the payments module"
    role: piinpoint-test-writer    # Specialized persona for test writing

  lint-cleanup:
    prompt: "Fix all lint errors"
    role: task-runner              # Minimal, focused persona

  pr-review:
    prompt: "Review the open PRs and leave detailed comments"
    role: code-reviewer            # Different instructions for review work
```

## Implementation

### `TaskConfig` field (`agentwire/tasks.py`)

```python
role: str | None = None  # Role override for this task
```

### Session creation in `_run_ensure_task()` (`agentwire/__main__.py`)

Tasks with `exit_on_complete: true` (default) always create fresh sessions. When
creating the session for the task, if `task.role` is set, pass it to `agentwire new`:

```python
if task.role:
    new_args += ["--roles", task.role]
```

This loads the role's markdown content as part of the agent's SYSTEM prompt.

### Note on scheduler.yaml

The scheduler already has `roles:` per task entry (at the scheduler level). The gap
is having it in `.agentwire.yml` task definitions for non-scheduled (queue-based) usage.
Both approaches coexist: scheduler `roles:` applies at session creation, task `role:`
applies within the ensure lifecycle.

## Files Modified

- `agentwire/tasks.py` — `role` field in `TaskConfig`
- `agentwire/__main__.py` — pass role to session creation in `_run_ensure_task()`

## Testing

```bash
# Create a custom role
mkdir -p ~/.agentwire/roles
echo "# Test Writer Role\nYou write thorough unit tests. Always use pytest." > ~/.agentwire/roles/test-writer.md

# Add to task
# role: test-writer in .agentwire.yml

agentwire ensure -s myproject --task write-tests
# Verify: session was created with test-writer role loaded
```

## Implementation Notes

The role is passed as a string to `NewArgs.roles` (not a list). `cmd_new()` expects
a comma-separated string format matching the `--roles agentwire,voice` CLI usage.

Role override only applies when the session is CREATED (i.e., `exit_on_complete: true`
or session doesn't exist). With `exit_on_complete: false`, the session persists across
task runs and keeps its original role.

Verified: task started with `agentwire` role shows no `--disallowedTools AskUserQuestion`
in the Claude startup command (task-runner role adds that flag, agentwire role does not).

## Done When

- [x] `role:` in task config passes role to session creation
- [x] Session loads the role's prompt content via `--system-prompt`
- [x] No `role:` → existing session default roles unchanged
- [x] Bug fix: pass role as string, not list (prevents silent ignore)
