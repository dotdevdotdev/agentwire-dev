# Fan-out cohorts — waiting on children without being reaped (#852)

A session that fans out child sessions has one problem nothing else in
agentwire solves: **idle ≠ done for a parent with outstanding children.** The
parent goes idle while *waiting*, the idle handler reads idle as done, and the
task is reaped — killing the roll-up the parent existed to write and orphaning
the children's report-backs.

A **cohort** is the ledger that makes "my children are still working" knowable.
Enrollment is automatic, so a fan-out is safe by default.

## The failure this fixes

`memory-manager`, 2026-08-01: a scheduled task audited the memory store, then
spawned one child per project needing review.

```bash
agentwire new -s memrev/<project> -p ~/projects/<project> --kind worker \
  --posture bypass --first-message "<review instructions>"
```

At the moment the parent was reaped, all four children were mid-turn. What
happened:

1. The roll-up never happened — the run reported `incomplete` although every
   child succeeded.
2. Each child's `notify-parent --on-idle --queued` hit a recipient that no
   longer existed. `done` is a load-bearing kind, so each dead-letter **emailed
   the owner** about work that had succeeded.
3. All four children stayed alive and idle afterwards, and nothing reaped them.
   They're full sessions, not worker panes, so the idle handler's pane path
   (summary → kill) never applied. A nightly fan-out leaking N sessions per run
   isn't shippable.

## Cohort is not rooting

`created_by` (#715) answers *"who has authority, where do prompts route"*. It
deliberately records **no parent for a cross-project spawn** — a session only
inherits its caller when the new session's project is the caller's own.

`memory-manager` runs in agentwire-dev and spawned into three other projects,
so only one of its four children would have carried the link. A guard derived
from `created_by` would have protected that one child and reaped the parent out
from under the other three — a **silently half-linked** fan-out, worse than an
unlinked one.

So cohort membership is keyed off the **caller**, independent of the rooting
decision:

| | ROOTING (`created_by`) | COHORT |
|---|---|---|
| Answers | authority + prompt routing | lifecycle ownership |
| Cross-project spawn | no parent recorded (#715) | still enrolled |
| Opt out | `--created-by ''` | `--no-cohort` |

## The ledger

`~/.agentwire/cohorts/<parent>.json`:

```json
{"parent": "memory-manager", "task": "memory-manager", "created_at": 1785595520,
 "deadline": 1785599120,
 "children": [{"session": "memrev-playchek", "state": "pending", "report": null}]}
```

States: `pending` → `reported` (report collected, child torn down), `timeout`
(deadline hit with no report; torn down anyway), `gone` (the child's session
vanished before it reported). `topology` (`main` | `worktree`) decides whether
resolving a child also kills its session; `torn_down` records what happened.

`state` and `deadline` are read by the idle-handler's `jq` as well as by
Python — renaming either silently disarms the guard.

**Enrollment is automatic.** `agentwire new` (and therefore `agentwire
worktree`) appends to the caller's ledger whenever there is a caller. The
failure being prevented is silent and unattended, so it must not depend on a
task author remembering to register anything. Two spawns opt out: `--no-cohort`
(a genuinely fire-and-forget spawn) and `--kind orchestrator` (durable by
definition, and rooted for the same reason).

## Three consumers

### 1. `agentwire wait --children` — the join primitive

```bash
agentwire wait --children --timeout 300     # MCP: wait_children(timeout=300)
```

Blocking here happens **inside a tool call, which is not idle** — the hook
never fires, and the agent resumes naturally to write its roll-up with every
report in hand. Bounded and re-callable: a fan-out longer than the calling
harness's tool timeout just loops. Exit status is 0 when the cohort resolved, 1
when children are still pending.

Each pass, per pending child:

- a report waiting in the parent's inbox is **consumed, then the child is torn
  down**;
- a child whose slot holds ONLY the idle handler's synthetic placeholder
  (`kind=idle`, "is idle and done working") is marked `resolved_idle`, **not**
  `reported` (#952): it went idle without saying anything, and `wait` says so
  (`N idle-without-report` plus a WARNING line) rather than counting silence
  as a report. The discriminator is the message KIND, never the text — a
  child that legitimately writes those same words in its own `done` report is
  still `reported`;
- a child whose session already vanished is marked `gone`;
- past the cohort deadline, whatever is left is torn down and marked `timeout`,
  and returned as a failure so the parent's summary can name it.

**Teardown skips worktree children.** A `worktree`-topology child holds a
branch and possibly an open PR, whose teardown follows merge verification
(#756), and its session is where a reviewer sends fix-ups — killing it would
trade a visible dangling PR for a silently destroyed working tree. It is still
enrolled, still collected, and still named when it goes silent; `wait` reports
it under `left_alive`, and an abandoned one is what `worktree --dangling`
already flags. The children of the 2026-08-01 leak were `agentwire new`
sessions — `main` topology — and those *are* torn down, because nothing else
ever reaps them.

Reports are read **straight off the inbox files**, not waited for as a paste
into the parent's box — the parent is mid-tool-call, and a long report pasted
into an input box is exactly the delivery path #851 covers. The drain
cooperates: while a child is still `pending`, its messages to the parent are
**held** (deferred with no attempt penalty, reason `cohort_held`) so the paste
can't race the collection and leave the child unresolved until its deadline.
The hold is bounded by the ledger — once the cohort resolves or the sweeper
drops it, anything left delivers normally.

> **Collect-then-kill is load-bearing.** `agentwire kill` runs
> `inbox.gc_sender()`, which dead-letters the killed session's still-pending
> load-bearing outbound **and emails the owner**. Kill-before-collect turns
> every child's `done` report into owner email.

### 2. The idle-handler guard — the safety net

A file-existence + `jq` check at the top of `idle-handler.sh`, the same shape
as the usage-limit park guard (#274) and the prompt-router guard (#276):
pending children and the deadline not yet passed → `exit 0`. It covers the
parent going idle *between* `wait` calls, which is where the silent version of
this failure lives.

It **fails open** on a missing, corrupt, or past-deadline ledger — a broken
ledger must never wedge a task alive forever.

### 3. The watchdog sweeper — the anti-leak backstop

On the existing 60s `agentwire limits tick`, after the zombie reap:

- **parent gone** → kill whatever is still pending (main topology only, per
  above) and delete the ledger. This is the crash path (usage limit, guard
  deadline, `/exit`) that leaks children under every other mechanism.
- **parent alive** → mark pending children whose session has vanished, so the
  guard self-clears as the cohort empties.
- **past deadline + grace** → the parent fanned out and never joined; reap the
  stragglers (same topology rule) and drop the ledger.

## Why teardown lives with the parent, not the child

The worker-pane lifecycle (idle #1 → write a summary, idle #2 → report and
self-kill) generalizes only halfway. Take the reporting half, not the killing
half:

- **A self-kill can't cover the failure path.** A wedged child never goes idle,
  so it never self-kills — the case that actually leaks stays leaking.
- **The scoping is dangerous.** It must fire for main-topology parented workers
  but *never* for worktree workers, whose branches and open PRs are torn down
  only after merge verification (#756). A misfire deletes work.
- **Single owner.** Collect-then-kill ordering is enforceable in one code path
  rather than raced between child and parent.

Cohort teardown is deliberately **session-only** and, for worktree children,
doesn't happen at all — a worktree's branch, open PR, and working tree stay
entirely with `agentwire worktree --remove`, whose guards split by what's at
risk (#941): worktree/session removal refuses a dirty tree (durability),
branch deletion demands a verified merge (integration).

## Reference

| Piece | Where |
|---|---|
| Ledger + join + sweep | `agentwire/cohort.py` |
| CLI | `agentwire/wait_cli.py` (`agentwire wait --children`) |
| MCP | `wait_children` (`agentwire/mcp_session.py`) |
| Enrollment | `agentwire/session_cli.py` (`cmd_new`) |
| Idle guard | `agentwire/hooks/idle-handler.sh` |
| Watchdog stage | `agentwire/limits_cli.py` (`cmd_limits_tick`) |
| Events log | `~/.agentwire/cohort-events.jsonl` |

Related: [prompt routing](prompt-routing.md) (#276 guard shape),
[polite messaging](messaging.md) (the inbox reports arrive through),
[worktree sessions](worktree-sessions.md) (#756 teardown policy).
