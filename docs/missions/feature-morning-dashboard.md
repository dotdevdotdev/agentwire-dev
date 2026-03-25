> Living document. Update this, don't create new versions.

# Mission: Morning Dashboard

## Goal

`agentwire scheduler report` generates an HTML artifact summarizing all overnight
task work: statuses, branches created, PRs opened, durations, summaries.

## Status: In Progress

## Use Case

```bash
# Morning: see what ran overnight
agentwire scheduler report --since 8h --artifact

# As a scheduled task (runs after all overnight tasks)
tasks:
  morning-report:
    project: ~/projects/piinpoint
    session: piinpoint-report
    post:
      - "agentwire scheduler report --since 12h --artifact"
    prompt: "Summarize what was accomplished overnight"
    schedule:
      after: [write-tests, lint-cleanup]
      delay: 5m
```

## Implementation

### New `cmd_scheduler_report()` in `agentwire/__main__.py`

```bash
agentwire scheduler report [--since 8h] [--artifact] [--open]
```

Steps:
1. Parse `--since` duration (default: 8h)
2. Load scheduler board state (board YAML) + event history
3. Filter events to the time window
4. For each task that ran: collect name, status, duration, summary, branch, PR URL
5. PR URL comes from summary files if Feature 1 writes it there
6. Generate HTML artifact: table with status badges, branch/PR links
7. Save to `~/.agentwire/artifacts/morning-report-{date}.html`
8. If `--artifact`: `run_agentwire_cmd(["open", "morning-report-{date}.html", "--title", "Morning Report"])`
9. Output artifact path + summary stats to stdout

### HTML structure

- Header with date/time and summary counts (N complete, N failed, N incomplete)
- Table: Task | Status | Branch | PR | Duration | Summary
- Color-coded status badges (green/red/yellow)
- Clickable PR links
- Minimal, clean CSS (dark-mode friendly)

### Summary file PR URL

Feature 1's `_create_task_pr()` appends `pr_url` to the task summary YAML front matter
so the morning report can collect it without re-querying GitHub.

### Subparser registration

Add `report` to the `scheduler` subparser group in the CLI argparse setup.

## Files Modified

- `agentwire/__main__.py` — `cmd_scheduler_report()`, argparse registration

## Testing

```bash
# After some scheduler tasks have run:
agentwire scheduler report --since 8h
# Should output: path to generated HTML, summary counts

agentwire scheduler report --since 8h --artifact
# Should open in portal desktop
```

## Done When

- [ ] `agentwire scheduler report` generates HTML artifact
- [ ] Shows all tasks from the time window with status/branch/PR/summary
- [ ] `--artifact` opens in portal
- [ ] PR URLs shown when available (from Feature 1 summary data)
- [ ] No tasks in window → graceful empty state HTML
