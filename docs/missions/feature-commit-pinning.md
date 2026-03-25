> Living document. Update this, don't create new versions.

# Mission: Commit Pinning in Session Fork

## Goal

Allow `agentwire fork` and the `session_fork()` MCP tool to accept a `--commit` parameter
so forked worktrees start from a specific commit/ref rather than HEAD.

## Status: In Progress

## Use Case

When spawning parallel overnight agents that must all start from the same baseline,
fork each from a pinned commit so no task starts from a different HEAD if another
task advanced the branch:

```bash
agentwire fork piinpoint piinpoint/write-tests --commit abc123
agentwire fork piinpoint piinpoint/lint-cleanup --commit abc123
```

```python
# MCP
session_fork(session="piinpoint", target="piinpoint/write-tests", commit="abc123")
session_fork(session="piinpoint", target="piinpoint/lint-cleanup", commit="abc123")
```

## Implementation

### `ensure_worktree()` (`agentwire/worktree.py`)

Add optional `commit: str | None = None` parameter.

Git supports this natively: `git worktree add -b <branch> <path> <commit>`.

For new branches:
```bash
git worktree add -b branch path <commit>
```

For existing branches (need post-checkout):
```bash
git worktree add branch path
git -C path checkout <commit>  # detached HEAD at commit
```

### `cmd_fork()` argparse (`agentwire/__main__.py`)

```python
fork_parser.add_argument(
    "--commit",
    metavar="REF",
    help="Fork from this commit/ref instead of HEAD (e.g. abc123, main~5)",
)
```

Pass `commit=args.commit` through to `ensure_worktree()`.

### `session_fork()` MCP tool (`agentwire/mcp_server.py`)

```python
def session_fork(session: str, target: str, commit: str = "") -> str:
```

Pass `--commit commit` to CLI if non-empty.

## Files Modified

- `agentwire/worktree.py` — `ensure_worktree()` commit param
- `agentwire/__main__.py` — fork argparse + pass-through
- `agentwire/mcp_server.py` — `session_fork()` commit param

## Testing

```bash
# Fork from 3 commits back
agentwire fork myproject myproject/test-branch --commit HEAD~3
# Verify: worktree is on the correct older commit
cd ~/projects/myproject-worktrees/test-branch && git log --oneline -1
```

## Done When

- [ ] `agentwire fork --commit REF` creates worktree at that commit
- [ ] `session_fork(commit="...")` MCP tool passes it through
- [ ] No `--commit` → existing HEAD behavior unchanged
- [ ] Invalid commit ref → clear error, no partial worktree left behind
