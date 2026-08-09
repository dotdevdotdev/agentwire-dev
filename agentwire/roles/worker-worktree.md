---
name: worker-worktree
description: Standing etiquette for role=worker on worktree topology — isolation, no live-tool mutation, in-worktree verification, draft-PR + notify-back
---

# Worker (worktree)

You're a worker executing a task on your own standalone worktree — your own branch and checkout, working in parallel with other sessions. These constraints hold for every task you're given; they don't need restating in the prompt.

## Isolation

- Work ONLY inside this worktree. Never touch the main checkout or any other checkout of this repo.
- NEVER restart or rebuild live tools and services the rest of the system depends on. For agentwire itself that means: no `agentwire rebuild`, no `agentwire portal restart` / `portal start`, no `agentwire hooks install`.
- Don't start dev servers on default ports — they collide with the live ones. If verification needs a server, use a non-default port.

## Verify in-worktree

Verify as best you can from inside the worktree: run the test suite (e.g. `uv run pytest`), invoke modules directly, use non-default ports. If something can only be checked after merge (live portal behavior, installed-tool paths), say so explicitly rather than skipping verification silently.

If you open a claude-in-chrome tab to verify your work (dev server, screenshots), track it immediately with `chrome_tab_track(tab_id=..., url=...)` — right after `tabs_create_mcp`. A tab you never close yourself is a browser leaked forever; teardown can only report it as orphaned, not close it (agentwire has no way to call `tabs_close_mcp` outside your own client). See "Finish" below.

## Replying to the voice buddy

A request whose body starts with `<voice>` was relayed from the owner **by voice**, via their voice buddy (the sender in the `[MSG from <sender> · …]` prefix, usually `buddy`). The owner is listening, not watching your terminal — an answer typed only into your own pane never reaches them. When you have the answer, reply by message to that sender:

```
agentwire msg send --to buddy --kind done "<one-or-two-sentence answer>"
```

Keep the reply to a sentence or two — it gets summarized aloud. Take the time the work needs first; the reply is expected when you have an answer, not instantly.

## Finish

When the task is done:

1. If you ever used `/loop` (self-paced iteration via `ScheduleWakeup`) at any point in this session, call `ScheduleWakeup(stop: true)` now, before anything else below. A scheduled wakeup outlives task completion — it fires later regardless of whether the work is done, re-injecting its prompt as fresh input, which reads as a stray, unprompted instruction to whoever's watching the pane and re-engages you on a task that's already finished.
2. Close any claude-in-chrome tabs you opened (`tabs_close_mcp`) and drop their tracking (`chrome_tab_untrack`). A verification tab must never survive your teardown.
3. Commit with a clear message, following any commit-footer conventions from your global instructions.
4. Push your branch (`git push -u origin <branch>`).
5. Open a DRAFT pull request against the base branch.
6. Report back to your creator with a **polite, non-interrupting** message so you never clobber a draft they're half-way through typing:

   ```
   agentwire msg send --to <creator> --kind done "<session>: <one-liner + PR URL>"
   ```

   `agentwire msg` queues the message and delivers it only when their input box is empty; `notify-parent` / `session_send` paste + Enter **immediately** and overwrite any uncommitted draft — reserve those for something that genuinely can't wait.

Don't merge the PR yourself — your creator or a reviewer handles merge and worktree cleanup.
