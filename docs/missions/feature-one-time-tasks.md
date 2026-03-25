> Living document. Update this, don't create new versions.

# Mission: One-Time Task Queue

## Goal

Scheduler tasks can declare `max_runs: N` or `once: true` to auto-disable after a
set number of successful runs. Enables "queue up tonight's work" without manual
enable/disable.

## Status: In Progress

## Use Case

```yaml
# In ~/.agentwire/scheduler.yaml
tasks:
  tonight-feature-scaffold:
    project: ~/projects/piinpoint
    session: piinpoint
    task: scaffold-payments
    once: true                  # Run once, then auto-disable
    schedule:
      every: 1m                 # Run ASAP

  quarterly-analysis:
    project: ~/projects/data
    session: analyst
    task: q1-report
    max_runs: 3                 # Run 3 times, then auto-disable
    schedule:
      every: day
      at: "08:00"
```

## Implementation

### `SchedulerTask` fields (`agentwire/scheduler.py`, ~line 75)

```python
max_runs: int | None = None   # Auto-disable after N successful dispatches
once: bool = False            # Shorthand for max_runs: 1
```

### Parsing in `load_board()` (~line 217)

```python
max_runs=t.get("max_runs"),
once=bool(t.get("once", False)),
```

Post-load normalization: if `task.once and task.max_runs is None`, set `task.max_runs = 1`.

### Eligibility check in `pick_next_task()` (~line 568)

Early-exit before computing eligible timestamp:
```python
if task.max_runs is not None and state.run_count >= task.max_runs:
    continue
```

### Auto-disable in `dispatch_task()` (~line 1088)

After `run_count` increment:
```python
if task.max_runs is not None and new_state.run_count >= task.max_runs:
    task.enabled = False
    _log_event("task_disabled", task=task_name, reason="max_runs_reached",
                run_count=new_state.run_count)
```

### Board display

`get_board_display()` row dict gains `"max_runs"` and `"once"` fields.
Board CLI shows remaining runs where applicable.

## Infrastructure Note

`run_count` already exists in `TaskState` and is already persisted to/from board YAML
and incremented after each successful dispatch. This is a small addition on top of
existing infrastructure.

## Files Modified

- `agentwire/scheduler.py` — SchedulerTask fields, load_board(), pick_next_task(), dispatch_task(), get_board_display()

## Testing

```bash
# Add a once: true task to scheduler.yaml
# Run scheduler, verify task dispatches once
# Check board — task should be disabled
agentwire scheduler board
# Should show task as disabled with run_count: 1
```

## Done When

- [ ] `once: true` task auto-disables after first successful dispatch
- [ ] `max_runs: N` task auto-disables after N successful dispatches
- [ ] Board shows remaining runs for limited tasks
- [ ] Scheduler event logged when task auto-disables (`max_runs_reached`)
- [ ] Tasks at limit are skipped without error (graceful)
- [ ] Re-enabling a disabled task resets the limit check (respects run_count vs max_runs)
