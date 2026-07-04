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
3. config `worktree.default_base`
4. **the repo's actual default branch** — `git symbolic-ref refs/remotes/origin/HEAD` (e.g. a monorepo defaulting to `develop`), falling back to the current branch, finally `main`

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
agentwire worktree --remove name   # kill the session + remove the worktree + unregister
agentwire worktree --prune         # drop entries whose worktree is gone + `git worktree prune`
```

`--list` annotates each entry: **live** (tmux session running), **orphan** (worktree on disk, no session), **stale** (registry entry, worktree gone). `--remove` is the cleanup/recovery path; it still works on hand-created worktrees not in the registry by falling back to the conventional `<worktree_dir>/<project>/<name>/` layout. Removing (or pruning) a project's last worktree also removes the now-empty `<worktree_dir>/<project>/` dir.

## Config

```yaml
worktree:
  worktree_dir: ~/worktrees       # worktrees nest per project: <worktree_dir>/<project>/<name>/
  default_base: develop           # omit → repo-derived (origin/HEAD)
  default_project: ~/projects/my-repo
  naming: "{user}/{slug}"
```

Distinct from `projects.worktrees` (the legacy `project/branch` session layout under `~/projects/<project>-worktrees/`). See the `agentwire-config` skill for the full field reference.

## Other modes

```bash
agentwire worktree feature/auth --existing   # checkout an existing branch (no new branch)
agentwire worktree review-v2 --ref v2.0.0     # detached at a tag/commit/branch
agentwire worktree foo --current              # base off the repo's current branch
```
