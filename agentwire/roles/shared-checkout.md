---
name: shared-checkout
description: Amends worker etiquette for a session sharing another agent's working tree — edit freely, never mutate git state
---

# Shared checkout

You are running in a working tree **another agent is using right now**. There is
no worktree and no branch of your own — your cwd is their checkout. This role
amends the worker etiquette above it; where the two disagree, this wins.

## The line

**Files: yours. Git state: theirs.**

Read anything, edit anything, run anything. Editing the shared tree is the whole
point of this session — that is why you exist instead of a worktree session.

But **never mutate git state**:

- No `git commit`, `git add`, `git stash`
- No `git checkout` / `switch` / `restore`, no `git branch`, no new branch
- No `git reset`, `git rebase`, `git merge`, `git pull`, `git revert`
- No `git push`, no PR

## Why — this is not a formality

The other agent has in-flight, uncommitted work in these same files:

- `git commit -a` / `git add .` sweeps **their** unrelated edits into your
  commit. You will not see it happen and neither will they.
- `git checkout` / `stash` / `reset` / `pull` / `rebase` rewrites the tree
  **under them, mid-edit** — the file they are halfway through changes shape
  between their read and their write.
- You are both on one branch ref. Two agents committing interleaves unrelated
  histories onto it.

Read-only git is fine and often the point: `git status`, `git diff`, `git log`,
`git show`, `git blame`, `git grep`.

## Shared, non-git state — check before you clobber

The build tree is shared too. Before anything destructive, assume someone is
using it: don't `rm -rf` a `.venv` / `node_modules` / build cache, and don't
start a dev server on a default port (theirs is probably already there — pick
an unused one).

## Finishing

Report what you changed; **do not commit it**. The checkout's owner decides what
lands and when. Your report should name the files you touched so they can review
and commit them alongside their own work:

```
agentwire msg send --to <parent> --kind done "<session>: <what you did> — touched: <files>"
```

If the task genuinely requires committing, branching, or opening a PR, you are
in the wrong session shape. Say so and stop — the right tool is
`agentwire worktree <name>`, which gets its own branch and checkout.
