> Living document. Update this, don't create new versions.

# Mission: Starting Session / Context Inheritance

## Goal

Tasks can declare `starting_session: <name>` to fork that session's Claude conversation
context into the task's session before running, so the agent starts with pre-loaded
domain knowledge instead of a cold start.

## Status: In Progress

## Use Case

```yaml
tasks:
  continue-payments-refactor:
    prompt: "Continue the payments refactor from where we left off"
    starting_session: payments-loaded     # Fork Claude context from here
    starting_ref: feature/payments        # Also start from this branch
```

When the task runs:
1. Fork `payments-loaded` session → task's target session
2. The fork copies Claude's conversation JSONL history
3. Task runs in forked session — agent has full context

## Implementation

### `TaskConfig` field (`agentwire/tasks.py`)

```python
starting_session: str | None = None  # Fork Claude context from this session
```

### Lifecycle in `_run_ensure_task()` (`agentwire/__main__.py`)

After session existence check, before pre-commands:

```python
if task.starting_session and task.starting_session != session_name:
    _fork_starting_session(task.starting_session, session_name)
```

New `_fork_starting_session(source, target)`:
1. Check source session exists in tmux (`agentwire list`)
2. If yes: `run_agentwire_cmd(["fork", "-s", source, "-t", target])`
   - This copies Claude conversation history files
3. If source not found: log warning, continue with fresh session (graceful degradation)

### Interaction with `starting_ref`

Both can be used together:
- `starting_session` forks Claude conversation context
- `starting_ref` sets up the git branch

Order: fork session first (step A), then setup branch (step B) so the agent starts
with both right context and right branch.

## Files Modified

- `agentwire/tasks.py` — `starting_session` field in `TaskConfig`
- `agentwire/__main__.py` — `_fork_starting_session()`, wired into `_run_ensure_task()`

## Testing

```bash
# Have an active session with context loaded
agentwire new -s context-loaded -p ~/projects/myproject
# Send it some context
agentwire send -s context-loaded "You are working on the payments module. Key files: ..."

# Create task that uses it
# starting_session: context-loaded in .agentwire.yml task

agentwire ensure -s myproject --task continue-work
# Verify: new session was forked from context-loaded
# Verify: Claude in new session has prior conversation available
```

## Done When

- [ ] `starting_session` forks conversation context before task prompt is sent
- [ ] Source session not found → warning logged, fresh session used (no hard failure)
- [ ] Works with `starting_ref` (both can be set)
- [ ] No `starting_session` → existing behavior unchanged
