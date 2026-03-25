> Living document. Update this, don't create new versions.

# Mission: Branch Management in Task Config

## Goal

Allow tasks to declare `starting_ref`, `work_branch`, `pr_target`, and `pr_draft` so
the task lifecycle handles all git branch plumbing automatically. Eliminates custom
scripts for the overnight async agent workflow.

## Status: In Progress

## Use Case

```yaml
tasks:
  write-tests:
    prompt: "Write missing unit tests for recent changes"
    starting_ref: main
    work_branch: agent/write-tests    # default: agent/<task>-<YYYY-MM-DD>
    pr_target: main                   # default: starting_ref
    pr_draft: true                    # default: true
```

## Implementation

### TaskConfig fields (`agentwire/tasks.py`)

```python
starting_ref: str | None = None   # Any valid git ref (branch, SHA, tag)
work_branch: str | None = None    # Branch name; default: agent/<task>-<YYYY-MM-DD>
pr_target: str | None = None      # PR target branch; default: starting_ref
pr_draft: bool = True             # Create as draft PR
```

### Pre-task lifecycle step — `_setup_task_branch()`

Runs after session check, before pre-commands:

1. Verify `starting_ref` exists (`git rev-parse --verify`) — fail task (exit 4) if not
2. `git checkout starting_ref`
3. If it's a branch (not detached HEAD): `git pull --ff-only`
4. Compute `work_branch` if not set: `agent/<task.name>-<YYYY-MM-DD>`
5. If `work_branch` already exists: find first unused `work_branch-2`, `-3`, etc.
6. `git checkout -b work_branch`

### Post-task lifecycle step — `_create_task_pr()`

Runs after post-commands, before session exit:

1. Check for uncommitted changes: `git status --porcelain`
2. If changes: `git add -A && git commit -m "chore: agent task <task.name>"`
3. `git push -u origin work_branch`
4. `gh pr create --base pr_target --head work_branch --title "..." --body "..." [--draft]`
   - PR title: `"agent: <task.name> on <date>"`
   - PR body: task summary from summary file
   - Store PR URL in summary data for morning dashboard
5. If no changes: skip commit/push/PR; log note
6. `git checkout starting_ref` — reset working state

### Edge cases

- `starting_ref` not found → `PreCommandError` (exit code 4)
- Work branch collision → auto-increment suffix
- No changes → graceful skip, note in output
- `gh` not in PATH → log warning, skip PR (don't fail task)
- Push/PR fails → log error, task status remains `complete`

## Files Modified

- `agentwire/tasks.py` — TaskConfig fields, parse_task_config()
- `agentwire/__main__.py` — `_setup_task_branch()`, `_create_task_pr()`, `_run_ensure_task()`
- `CLAUDE.md` — Task schema docs

## Testing

```bash
# Create a test task in a git repo
cat > /tmp/test-project/.agentwire.yml << 'EOF'
tasks:
  test-branch-task:
    prompt: "Create a file called test-output.txt with today's date"
    starting_ref: main
    pr_target: main
    pr_draft: true
EOF

agentwire ensure -s test-project --task test-branch-task
# Verify: branch agent/test-branch-task-YYYY-MM-DD created
# Verify: draft PR opened against main
```

## Done When

- [ ] `starting_ref` triggers branch setup before task runs
- [ ] `work_branch` is created (with auto-dedup if exists)
- [ ] Changes are committed and pushed after task completes
- [ ] Draft PR is created via `gh`
- [ ] PR URL appears in task output / summary
- [ ] No `starting_ref` → existing behavior unchanged
- [ ] `gh` missing → graceful warning, no failure
