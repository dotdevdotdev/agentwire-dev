---
name: worktree-session
description: Standing etiquette for standalone worktree sessions — isolation, no live-tool mutation, in-worktree verification, draft-PR + notify-back
---

# Worktree Session

You're a standalone worktree session — your own branch and checkout, working in parallel with other sessions. These constraints hold for every task you're given; they don't need restating in the prompt.

## Isolation

- Work ONLY inside this worktree. Never touch the main checkout or any other checkout of this repo.
- NEVER restart or rebuild live tools and services the rest of the system depends on. For agentwire itself that means: no `agentwire rebuild`, no `agentwire portal restart` / `portal start`, no `agentwire hooks install`.
- Don't start dev servers on default ports — they collide with the live ones. If verification needs a server, use a non-default port.

## Verify in-worktree

Verify as best you can from inside the worktree: run the test suite (e.g. `uv run pytest`), invoke modules directly, use non-default ports. If something can only be checked after merge (live portal behavior, installed-tool paths), say so explicitly rather than skipping verification silently.

## Finish

When the task is done:

1. Commit with a clear message, following any commit-footer conventions from your global instructions.
2. Push your branch (`git push -u origin <branch>`).
3. Open a DRAFT pull request against the base branch.
4. Report back to your creator with a **polite, non-interrupting** message so you never clobber a draft they're half-way through typing:

   ```
   agentwire msg send --to <creator> --kind done "<session>: <one-liner + PR URL>"
   ```

   `agentwire msg` queues the message and delivers it only when their input box is empty; `notify-parent` / `session_send` paste + Enter **immediately** and overwrite any uncommitted draft — reserve those for something that genuinely can't wait.

Don't merge the PR yourself — your creator or a reviewer handles merge and worktree cleanup.
