# Worktree sessions

> The first-class primitive for "spawn an isolated branch + worktree + session for one unit of work." This is **session orchestration, not project management** — the unit of work is an opaque branch/name; agentwire neither knows nor cares whether it maps to a GitHub issue, a kanban card, or nothing.

```bash
agentwire worktree fix-bug          # new branch from the repo's default base + standalone session
```

A worktree session is a **standalone tmux session** (`{project}-{name}`) running on its own git worktree at `<worktree_dir>/<project>/<name>/` (default `~/worktrees/<project>/<name>/` — nested per project, mirroring `~/projects/<project>/`; the tmux session name stays flat). It survives independently of its creator and carries the intrinsic **worktree-session etiquette** (isolation, no live-tool rebuild/restart, in-worktree verification, draft PR + notify-back) — that role is injected by the spawn verb, so first prompts only need the task itself. `--roles` / `.agentwire.yml roles:` **add** to it; they never replace it.

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

agentwire keeps a small **local, per-repo registry** (one JSON file per repo under `~/.agentwire/worktrees/`, keyed by branch) recording `branch → session, base, worktree path, created-at`. It's populated on spawn and is **agentwire-owned local state — never provider data**. The files are plain JSON and hand-editable.

```bash
agentwire worktree --list          # this repo's worktree sessions (live / orphan / stale)
agentwire worktree --list --all    # across every repo
agentwire worktree --remove name   # kill the session + remove the worktree + branch + unregister
agentwire worktree --prune         # drop entries whose worktree is gone + `git worktree prune`
```

`--list` annotates each entry: **live** (tmux session running), **orphan** (worktree on disk, no session), **stale** (registry entry, worktree gone). `--remove` is the cleanup/recovery path; it still works on hand-created worktrees not in the registry by falling back to the conventional `<worktree_dir>/<project>/<name>/` layout. Removing (or pruning) a project's last worktree also removes the now-empty `<worktree_dir>/<project>/` dir.

### Teardown is atomic (#717)

`--remove` kills the tmux session, force-removes the git worktree (`git worktree remove --force` + `git worktree prune`), and only THEN drops the registry entry — it never touches `main` or requires switching the primary checkout's branch, so it works even when `~/projects/<repo>` permanently holds `main`. If the directory somehow can't be cleared (e.g. its `.git` link is broken), the command fails LOUDLY — non-zero exit, `success: false`, the reason in `error` — and the registry entry is **kept** so `--list`/`--prune` still see it. It never silently "unregisters" an orphaned directory.

`--remove` also best-effort deletes the branch — local ref (`git branch -D`) and, if it was pushed, the remote (`git push origin --delete`) — but **only once the branch is confirmed merged**, so a teardown can never silently drop unmerged work. "Merged" is checked via `gh pr view <branch> --json state,headRefOid` first (catches squash/rebase merges, whose commit hash differs from the branch tip so a plain git ancestor check would miss them), falling back to a `git merge-base --is-ancestor` check against `origin/<base>` when `gh` is unavailable/unauthenticated or no PR was ever opened. The gh path also cross-checks `headRefOid` against the branch's actual current tip SHA before trusting a MERGED verdict — `gh pr view <branch>` resolves by head-branch **name**, so a long-merged PR whose remote branch was since deleted could otherwise be mistaken for a brand-new branch that happens to reuse the same name (agentwire's own worktree naming defaults recur: `fix-bug`, `cleanup`, ...), force-deleting real unmerged work under that name. Flags:

```bash
agentwire worktree --remove name --keep-branch          # skip branch cleanup entirely
agentwire worktree --remove name --force-delete-branch  # delete even if not confirmed merged
```

### Browser verification tabs are torn down too (#717)

Worktree sessions often open a claude-in-chrome tab to verify their work (dev server, screenshots) before opening a PR. Two MCP tools track that so it doesn't leak: `chrome_tab_track(tab_id, url)` (call right after `tabs_create_mcp`) and `chrome_tab_untrack(tab_id)` (call after you close it yourself with `tabs_close_mcp`). agentwire has no way to call `tabs_close_mcp` itself — that MCP server runs inside the calling agent's own client, not agentwire's process — so this is pure bookkeeping, not automatic closing.

The **normal path** is the session closing its own tabs (and untracking them) before finishing — see the `worktree-session` role's Finish etiquette. The **crash backstop**: `--remove` (and `--prune --gc-merged`) checks this registry during teardown and reports any tab a session never got around to closing, so the calling agent can close it via `tabs_close_mcp`. `chrome_tab_list` shows what's currently tracked, across sessions or for one.

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
