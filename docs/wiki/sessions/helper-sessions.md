# Helper sessions (no isolation)

`agentwire helper <name>` creates a worker **session** that shares the caller's
checkout — no `git worktree add`, no branch, no directory, no registry entry.

This is the one thing a worker pane could do that a worktree session couldn't
(#838). Everything else a pane offered was a regression: no `agentwire msg`
inbox (#834), no voice, a headless exit-summary instead of report-back, no
portal visibility. A helper is a real session, so it gets all of those for free.

```bash
agentwire helper digest --prompt "Run the full suite and tell me what fails"
agentwire helper scout -p ~/projects/other-repo --prompt "Map the auth flow"
```

## What it is

| | `agentwire worktree` | `agentwire helper` |
|---|---|---|
| Directory | new, `~/worktrees/<project>/<name>/` | **the caller's own checkout** |
| Branch | new (or `--existing` / `--ref`) | **none** |
| Git work at creation | `fetch` + `worktree add` + `checkout -b` + seed | **none** |
| Worktree registry | registered | **not registered** — no git resource exists |
| Teardown | `worktree --remove` (after merge verification, #756) | `agentwire kill` |
| Cohort topology | `worktree` → `wait` leaves it alive | `main` → `wait` collects the report, then reaps it |
| Role | `worker-worktree` | `worker` + `shared-checkout` |
| msg inbox / voice / portal | yes | yes |

Rooting, prompt routing, and `notify-parent` behave identically — a helper is
by definition in the caller's own project, so it's parented like any
same-project spawn (#715).

## The safety model

Two agents in one working tree is a real footgun, which is why
`session_cli.cmd_new` has a shared-working-dir guard (#854/#857). `helper` does
**not** weaken that guard — it *declares* past it with `allow_shared_dir=True`,
the same posture `services.py` takes ("registering a service is explicit
intent"). Interactive `agentwire new` is unaffected: it still refuses.

What makes the sharing safe is the constraint the `shared-checkout` role puts on
the agent:

> **Files: yours. Git state: theirs.**

Read and edit freely — editing the shared tree is the entire point. Never
`commit`, `add`, `stash`, `checkout`, `branch`, `reset`, `rebase`, `pull`, or
`push`. Concretely, on a tree someone else has in-flight work in:

- `git commit -a` / `git add .` sweeps **their** unrelated edits into your commit.
- `checkout` / `stash` / `reset` / `pull` / `rebase` rewrites the tree under
  them, mid-edit.
- You share one branch ref, so two committers interleave unrelated histories.
- `.venv`, `node_modules`, build caches and fixed-port dev servers are shared
  and raceable.

`shared-checkout` stacks *after* the non-overridable `worker` rail
(`["worker", "shared-checkout", …your --roles]`), so its "never mutate git
state" corrects `worker.md`'s "commit your work" by recency weight. Your
`--roles` stack on top of both; they never replace either.

**If the task needs a commit, a branch, or a PR, it's the wrong session shape** —
use `agentwire worktree <name>`.

## Why nothing is written to the worktree registry

The registry exists to catch one failure: a directory and a branch left on disk
that nothing tracks (#837). A helper creates neither, and `agentwire kill`
reclaims 100% of it.

An entry pointing at the repo's own checkout would be a resource that doesn't
exist, and the destructive verbs would eventually act on it: `--remove` resolves
through `find_git_worktree`, which deliberately never returns the main checkout
(#855/#862), so it could never resolve; `--prune` drops entries whose path is
gone, and the main checkout never is, so the entry would accumulate forever.

Helpers are visible where sessions are visible — `agentwire sessions`, the
portal sidebar, `session_created` events. That's the session list; the worktree
registry is not the session list.

## Cost

Measured on `agentwire-dev` (555 tracked files), warm:

| | `agentwire worktree` | `agentwire helper` |
|---|---|---|
| `git fetch origin <base>` | ~750–900 ms (network) | — |
| `git worktree add` | ~500–550 ms | — |
| `git checkout -b` | ~170–260 ms | — |
| seed gitignored files | ~50 ms | — |
| **git total** | **~1.5–1.7 s** | **0 ms, 0 commands** |
| files written to disk | 555 | 0 |

Both then pay the same `tmux new-session`. The gap is structural, not tuning:
the fetch is an unbounded network round-trip and `worktree add` is a full
checkout. Use `worktree` when you need isolation — that cost buys a branch you
can PR. Use `helper` when you don't.

## See also

- [Worktree sessions](worktree-sessions.md) — the isolated sibling
- [Fan-out cohorts](fan-out-cohorts.md) — `wait --children` reaps helpers, leaves worktrees alive
- [Polite messaging](messaging.md) — how a helper reports back
