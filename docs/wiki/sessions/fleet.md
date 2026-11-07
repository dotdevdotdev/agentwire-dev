# Fleet view (`agentwire fleet`)

One read-only view of every session an orchestrator spawned (#1277). Delegation
failed silently three ways (no parent #1259, wrong parent name #1261,
dead-lettered reports) and each was found hours later. The data already exists;
`agentwire/fleet.py` is the one reader that joins it.

```bash
agentwire fleet                 # the current session's children
agentwire fleet -s <session>    # a named orchestrator
agentwire fleet --all --json    # every cohort ledger on the machine
agentwire fleet --no-pr         # skip the GitHub lookup (offline / fast)
```

## Single source of truth

`fleet.build_fleet()` → `agentwire fleet --json` verbatim. `doctor` (the
"Checking the fleet" section) renders its `flagged` / `broken_ledgers`; the
portal (`GET /api/fleet` → `agentwire fleet --all --json`, sidebar **Fleet**
section) renders the same JSON. Neither decides a flag itself.

## Where each field comes from

| Field | Source |
|-------|--------|
| child, parent, topology, ledger state | cohort ledger (`~/.agentwire/cohorts/`) |
| state `running`/`idle`/`gone` | tmux liveness + ledger `resolved_idle` |
| role / roles, `session_record` | session launch record (`load_session_metadata`) |
| branch | worktree registry (worktree-topology entries) |
| PR | `session_cli.pr_for_branch` — the same probe the dangling-PR scan uses |
| `last_report_at` | ledger `resolved_at`, stamped when a child resolves as `reported` |
| `inbox_waiting_age_s` / `dead_letter_age_s` | the child's own inbox / `dead/` |
| `report_queued_age_s` / `report_dead_letter_age_s` | the child's reports in the **parent's** inbox / `dead/` |

## Flags

| Flag | Meaning |
|------|---------|
| `idle_no_report` | (live child, #1324: not raised once gone with no open PR) child went idle; only the idle handler's placeholder arrived — its work may not have happened |
| `report_queued_to_dead_session` | a report is queued for a parent that is not live; nothing will drain it |
| `report_dead_lettered` | a load-bearing message from the child was dead-lettered on its way to the parent |
| `gone_with_open_pr` | the child's session is gone but its branch still has an OPEN PR |
| `no_session_record` | a live child with no launch record (role/parent unknowable) |

## Guarantees

- **Reads only.** No ledger rewritten, no message touched, nothing killed (a test snapshots the tree before/after).
- **Absence is reported, not raised.** A corrupt ledger appears under `broken_ledgers` (and as `ledger: "corrupt: …"` on its parent row); a typo'd `-s` shows `ledger: "missing"`; zero ledgers is a valid empty fleet. Exit status is 0 even when flags are raised.
- Children enrolled before `resolved_at` existed show `last_report_at: null`.
