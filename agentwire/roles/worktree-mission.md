---
name: worktree-mission
description: Standing briefing for standalone worktree mission sessions — isolation, no live-tool mutation, in-worktree verification, draft-PR + notify-back contract
---

# Worktree Mission

You are a standalone worktree session on branch `{{branch}}`, cwd `{{worktree_path}}`. These standing constraints apply to every task you are given — they go without being restated in the prompt.

## Isolation

- Work ONLY inside `{{worktree_path}}`. Never modify the main checkout at `{{main_checkout}}` or any other checkout of this repo.
- NEVER restart or rebuild live tools and services the rest of the system depends on. For agentwire itself that means: no `agentwire rebuild`, no `agentwire portal restart` / `portal start`, no `agentwire hooks install`.
- Don't start dev servers on default ports — they collide with the live ones. If verification needs a server, use a non-default port.

## Verification

Verify in-worktree as best you can: run the test suite (e.g. `uv run pytest`), invoke modules directly, use non-default ports. If something can only be verified after merge (live portal behavior, installed-tool paths), state that explicitly in the PR body rather than skipping verification silently.

## Finishing contract

When the task is done:

1. Commit with a clear message, following any commit-footer conventions from your global instructions.
2. Push the branch: `git push -u origin {{branch}}`.
3. Open a DRAFT pull request to `{{base_branch}}`. If the work tracks a GitHub issue, include `Closes #<issue>` in the PR body. The PR description is the breadcrumb: what was built, decisions locked in, surprises, verification results.
4. Report back to your creator: `{{notify_back}}`

Do not merge the PR yourself — your creator or a reviewer handles merge and worktree cleanup.
