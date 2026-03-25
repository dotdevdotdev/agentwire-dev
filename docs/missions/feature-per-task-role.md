> Living document. Update this, don't create new versions.

# Mission: Per-Task Role Override

## Goal

Tasks can declare `role: <rolename>` to run with a specific role, overriding the
session's default roles. The role is applied when the session is created/recreated
for the task.

## Status: In Progress

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

## Done When

- [ ] `role:` in task config passes role to session creation
- [ ] Session loads the role's prompt content
- [ ] No `role:` → existing session default roles unchanged
- [ ] Invalid role name → warning logged, task continues with defaults
