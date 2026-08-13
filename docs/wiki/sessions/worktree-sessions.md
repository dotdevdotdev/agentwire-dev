# Worktree sessions

> The first-class primitive for "spawn an isolated branch + worktree + session for one unit of work." This is **session orchestration, not project management** — the unit of work is an opaque branch/name; agentwire neither knows nor cares whether it maps to a GitHub issue, a kanban card, or nothing.

```bash
agentwire worktree fix-bug          # new branch from the repo's default base + standalone session
```

A worktree session is a **standalone tmux session** (`{project}-{name}`) running on its own git worktree at `<worktree_dir>/<project>/<name>/` (default `~/worktrees/<project>/<name>/` — nested per project, mirroring `~/projects/<project>/`; the tmux session name stays flat). By default its ROLE is `worker` — a separate axis from topology (#716) — and it survives independently of its creator, carrying the intrinsic **worker-worktree etiquette** (isolation, no live-tool rebuild/restart, in-worktree verification, draft PR + notify-back) — that role is injected by the spawn verb's default, so first prompts only need the task itself. `--roles` / `.agentwire.yml roles:` **add** to it; they never replace it. Pass `--kind orchestrator` (or use the `agentwire orchestrator` sugar verb) instead, for a durable, replaceable-persona project window on this same worktree topology — see [role/topology axes](../../../CLAUDE.md#mcp-server-for-agents).

> "Worktree session" **always** means this command — never `agentwire spawn --branch` (that makes a worker *pane* inside the current session). See the [glossary](../glossary.md).

## Base branch — repo-derived, never hardcoded

The branch a new worktree forks from is resolved in this order:

1. `--base/-b <branch>` (explicit, always wins)
2. `--current/-c` (the repo's currently checked-out branch)
3. the project's `.agentwire.yml` `worktree.base` (per-project override, #705)
4. global config `worktree.default_base`
5. **the repo's actual default branch** — `git symbolic-ref refs/remotes/origin/HEAD` (e.g. a monorepo defaulting to `develop`), falling back to the current branch, finally `main`

So `agentwire worktree foo` in a repo whose default is `develop` branches off `origin/develop` with no flags and no config. (If `origin/HEAD` isn't set locally, run `git remote set-head origin -a` once to populate it; until then the current-branch fallback applies.)

## Project — inferred from cwd

`--project/-p` points at the git repo. When omitted, it resolves to config `worktree.default_project`, else the **git root of cwd** — so you can fire a worktree session from any subdirectory of a (mono)repo. Many worktree sessions can target the **same** repo from different branches; each is keyed by `name`.

## Branch naming templates

By default the git branch equals the CLI `name` verbatim. Set `worktree.naming` to honor a shop's branch convention without a wrapper script:

```yaml
worktree:
  naming: "{user}/{slug}"     # → "jordan/fix-bug"
  # or "feature-{slug}", etc.
```

Placeholders: `{name}` (verbatim), `{slug}` (slugified — lowercased, hyphenated), `{user}` (OS login). Only the **git branch** is templated; the tmux session name stays `{project}-{name}` (made tmux-safe) so session names remain predictable. Unknown placeholders are left literal rather than crashing on a hand-edited config.

## Branch↔session registry

agentwire keeps a small **local, per-repo registry** (one JSON file per repo under `~/.agentwire/worktrees/`, keyed by branch) recording `branch → session, base, worktree path, created-at, kind, topology`. It's populated on spawn and is **agentwire-owned local state — never provider data**. The files are plain JSON and hand-editable.

**Every creation site registers (#837).** Worktree creation used to live in five places with only `agentwire worktree` registering, so `agentwire spawn --branch`, `agentwire new -s project/branch` (which every scheduler worktree dispatch shells out to), `recreate`, and `fork` each produced a real git worktree that `--list`/`--dangling`/`--prune`/`--remove` could not see — an orphan by construction. They now all route through one helper (`worktree.create_and_register_worktree`), and registration is idempotent, so re-running a creation *heals* a missing entry rather than duplicating it.

**`topology` distinguishes the two shapes.** `worktree` means the recorded `session` **is** this worktree's session. `pane` means it's a worker pane's isolated branch from `agentwire spawn --branch`, where `session` is the **owning** session (whose pane 0 is an unrelated orchestrator) — so several pane entries legitimately share one session name, teardown never kills it, and `--dangling` skips them (a pane worker's parent is pane 0 of that same live session, so it can't be dangling). `--list` tags them `[pane worker]`.

```bash
agentwire worktree --list          # this repo's worktree sessions (live / orphan / stale)
agentwire worktree --list --all    # across every repo
agentwire worktree --remove name   # kill the session + remove the worktree + branch + unregister
agentwire worktree --prune         # drop entries whose worktree is gone + `git worktree prune`
```

`--list` annotates each entry: **live** (tmux session running), **orphan** (worktree on disk, no session), **stale** (registry entry, worktree gone). Removing (or pruning) a project's last worktree also removes the now-empty `<worktree_dir>/<project>/` dir.

### Paths come from git, never from the convention (#855)

`--remove`/`--status` resolve a name to a worktree by **asking git** (`git worktree list --porcelain`), in this order: registry entry (cross-checked against git, and *healed* to git's path if the recorded one drifted) → git's own worktree list → the documented layout as a last, explicitly-flagged guess.

This matters because the layout is a *default*, not a guarantee: `~/worktrees/<project>/<name>/` and `~/projects/<project>-worktrees/<name>/` are both live in the wild, and a hand-created worktree can sit anywhere. `--remove` used to reconstruct the conventional path, find nothing there, and **print a success line anyway** — so an operator believed a teardown had happened while the session, worktree and branch all survived, and the #756 merged-branch safety checks were skipped along with everything else.

Now, when nothing real resolves, `--remove` **fails loudly** (non-zero exit, `success: false`) and lists the worktrees git *does* know for that repo, instead of tearing down a guess. The JSON result carries `resolved_by` (`registry` / `git` / `convention`) and `worktree_existed`, so the human line says "no worktree at X (already gone)" rather than claiming a removal that didn't happen. `--status` likewise says "no worktree found" instead of printing a guessed path as though it were a real-but-missing one.

Session lookup is convention-blind too: the owning session is found by matching a live tmux pane's working directory against the resolved worktree path first, falling back to the `{project}-{name}` and `{project}/{name}` forms — #855's false success also named the wrong session (`agentwire-dev-fix-851-852` for a live `agentwire-dev/fix-851-852`).

### Dangling PRs — a different kind of orphan (#716)

`--list`'s "orphan" above means a **dead** session with a worktree dir still on disk. `agentwire worktree --dangling` (and `agentwire doctor`) flag the opposite failure: a **LIVE worker session** with an OPEN PR and no live parent — the concrete shape from #716, where a rooted-but-still-subordinate session correctly refuses to self-merge its own green PR, so it just dangles with nothing positioned to review/merge it. Orchestrator-kind entries are excluded entirely — a durable orchestrator roots by design and is itself the reviewer/merger, so a parentless orchestrator with an open PR is healthy, not dangling. It's a shallow "has any live parent" check (recorded creator, or the `.agentwire.yml parent:` fallback — the same precedence prompt-routing already uses) via `gh pr view <branch>` per live entry, not a full orchestrator-role verification of that parent (that's not durably stored anywhere and is out of scope — the deferred merge-authority-per-edge model is the eventual fuller fix).

`topology: pane` entries are skipped as well (#837) — a worker pane's parent *is* pane 0 of the very session on its entry, so the liveness gate already proves the parent is live.

```bash
agentwire worktree --dangling        # this repo
agentwire worktree --dangling --all  # every repo
```

`agentwire doctor` also sweeps for the plain **orphan** shape now that every creation site registers: a worktree still on disk whose owning session is gone. It reports only — teardown stays with `--remove`/`--prune`, where the #756 merged-branch guards live.

### Teardown asks two questions (#941)

Teardown safety was long written as one rule — "verify the PR is merged before tearing down" — but it is really two questions, and they authorize **different acts**:

1. **Is the work durable?** — committed (and, for anything a PR references, pushed), so the branch and PR exist independently of the session. This is what authorizes tearing down the **session and worktree**. Removing a worktree cannot destroy committed work — the branch ref lives on in the main repo — so the only thing at risk is *uncommitted changes*, and `--remove` refuses a dirty worktree (`--discard-changes` to override, destroying them deliberately).
2. **Has it been integrated?** — merged, the issue CLOSED. This is what authorizes deleting the **branch**, and it keeps all the guards described below.

Merge status is the common way of satisfying question 1, not the only way. A worker whose PR **by design never merges** — a spike branch, slice work PR'd into a spike branch — can never satisfy question 2, and holding its session open forever is pure cost. Once its work is committed and pushed, reap the session (`--remove --keep-branch` leaves branch and PR untouched); the branch-deletion guard is what still demands a verified merge.

### Teardown is atomic (#717)

`--remove` kills the tmux session, force-removes the git worktree (`git worktree remove --force` + `git worktree prune`), and only THEN drops the registry entry — it never touches `main` or requires switching the primary checkout's branch, so it works even when `~/projects/<repo>` permanently holds `main`. If the directory somehow can't be cleared (e.g. its `.git` link is broken), the command fails LOUDLY — non-zero exit, `success: false`, the reason in `error` — and the registry entry is **kept** so `--list`/`--prune` still see it. It never silently "unregisters" an orphaned directory.

`--remove` also best-effort deletes the branch — local ref (`git branch -D`) and, if it was pushed, the remote (`git push origin --delete`) — but **only once the branch is confirmed merged**, so a teardown can never silently drop unmerged work. "Merged" is checked via `gh pr view <branch> --json state,headRefOid` first (catches squash/rebase merges, whose commit hash differs from the branch tip so a plain git ancestor check would miss them), falling back to a `git merge-base --is-ancestor` check against `origin/<base>` when `gh` is unavailable/unauthenticated or no PR was ever opened. The gh path also cross-checks `headRefOid` against the branch's actual current tip SHA before trusting a MERGED verdict — `gh pr view <branch>` resolves by head-branch **name**, so a long-merged PR whose remote branch was since deleted could otherwise be mistaken for a brand-new branch that happens to reuse the same name (agentwire's own worktree naming defaults recur: `fix-bug`, `cleanup`, ...), force-deleting real unmerged work under that name. Flags:

```bash
agentwire worktree --remove name --keep-branch          # skip branch cleanup entirely
agentwire worktree --remove name --force-delete-branch  # delete even if not confirmed merged
agentwire worktree --remove name --force-delete-branch --close-pr-branch  # ...even with an OPEN PR
```

#### The orchestrator flow: ready → merge → verify → THEN teardown

Never teardown a worker's worktree before its work has actually landed. The order is: the worker reports **ready** (draft PR open, notified back) → the orchestrator **merges** the PR → the orchestrator **verifies** the merge landed (issue CLOSED via `Closes #N`, not just "PR shown green" — a draft PR or a still-open PR is not landed) → **only then** teardown. Tearing down first and checking later is how real work gets dropped. The exception is the never-merging branch above (#941): there, "landed" means committed-and-pushed, and the session is reaped with `--keep-branch` while the branch and PR persist.

#### `--force-delete-branch` does not bypass an OPEN PR (#756)

`--force-delete-branch` overrides the merge-state check for **plain unmerged/local-only work** — exactly as it always has. It does **not** also bypass an **OPEN PR** on that branch: deleting the remote head branch of an open PR silently closes it (GitHub loses the head ref), which is a different, more surprising destruction than dropping some unmerged commits nobody opened a PR for.

Before a force delete, `_delete_branch_if_safe` checks `gh pr view <branch> --json state,number` for an OPEN PR. If found, it refuses and names the PR:

```
branch fix-bug has OPEN PR #123 — force-deleting closes it. Merge/close the PR first, or pass --close-pr-branch to override.
```

The guard only fires on the force path — a branch that's already confirmed merged, or genuinely has no PR at all, deletes exactly as before. The explicit escape hatch is `--close-pr-branch`, for when you actually mean to close that PR. As with the rest of this cleanup, it's best-effort: no `gh` / no GitHub remote → can't check → proceeds like before the guard existed. The same guard covers `--prune --gc-merged` (shared `_teardown_entry` → `_delete_branch_if_safe`), though it's rarely hit there since that sweep only force-deletes branches it just confirmed merged.

**Real incident (2026-07-10)** that motivated this: an orchestrator tearing down a worker whose merge hadn't actually landed passed `--force-delete-branch` to silence gh's "branch used by worktree" warning, and it auto-closed the open PR. Recovery:

```bash
git fetch origin refs/pull/<N>/head   # pulls the PR's last commit down as FETCH_HEAD
git push origin FETCH_HEAD:refs/heads/<branch>   # recreates the branch GitHub needs to reopen onto
gh pr reopen <N>
```

### Browser verification tabs are torn down too (#717)

Worktree sessions often open a claude-in-chrome tab to verify their work (dev server, screenshots) before opening a PR. Two MCP tools track that so it doesn't leak: `chrome_tab_track(tab_id, url)` (call right after `tabs_create_mcp`) and `chrome_tab_untrack(tab_id)` (call after you close it yourself with `tabs_close_mcp`). agentwire has no way to call `tabs_close_mcp` itself — that MCP server runs inside the calling agent's own client, not agentwire's process — so this is pure bookkeeping, not automatic closing.

The **normal path** is the session closing its own tabs (and untracking them) before finishing — see the `worker-worktree` role's Finish etiquette. The **crash backstop**: `--remove` (and `--prune --gc-merged`) checks this registry during teardown and reports any tab a session never got around to closing, so the calling agent can close it via `tabs_close_mcp`. `chrome_tab_list` shows what's currently tracked, across sessions or for one.

```bash
agentwire tabs track --session name --tab-id <id> --url <url>   # bookkeeping only — CLI backing for chrome_tab_track
agentwire tabs untrack --session name --tab-id <id>
agentwire tabs list [--session name]
```

`--prune --gc-merged` extends the stale-entry sweep: for every **still-present** registered worktree whose branch is confirmed merged, it runs the same atomic teardown (session + worktree + branch). Plain `--prune` never does this on its own — it only drops entries whose directory is already gone — so a live, in-flight worktree is never touched just because its branch happens to look merged.

## Config

```yaml
worktree:
  worktree_dir: ~/worktrees       # worktrees nest per project: <worktree_dir>/<project>/<name>/
  default_base: develop           # omit → repo-derived (origin/HEAD)
  default_project: ~/projects/my-repo
  naming: "{user}/{slug}"
```

Distinct from `projects.worktrees` (the legacy `project/branch` session layout under `~/projects/<project>-worktrees/`). See the `agentwire-config` skill for the full field reference.

### Per-project overrides (#705)

A repo's `.agentwire.yml` can override the worktree root and base for **that project only**, so the global layout isn't a lock-in (the monorepo/develop-base shop is the canonical case):

```yaml
# .agentwire.yml at the repo root
worktree:
  dir: ~/work-trees     # overrides worktree.worktree_dir for this project
  base: develop         # overrides worktree.default_base for this project
```

Precedence (most specific wins): per-invocation `--base` flag → project `.agentwire.yml` `worktree:` block → global `config.yaml` `worktree:` → built-ins (`~/worktrees`, repo origin/HEAD). The nesting shape is unchanged — `dir` only moves the root: `<dir>/<project>/<name>/`.

All subcommands (create/list/status/remove/prune) resolve through the same project-scoped dir, so `--remove` always finds what create made. Registry entries record the **resolved** worktree path, so worktrees created before an override change remain listable and removable afterwards. Unknown keys in the block warn to stderr but never fail the config load.

## Other modes

```bash
agentwire worktree feature/auth --existing   # checkout an existing branch (no new branch)
agentwire worktree review-v2 --ref v2.0.0     # detached at a tag/commit/branch
agentwire worktree foo --current              # base off the repo's current branch
```
