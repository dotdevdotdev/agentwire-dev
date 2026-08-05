# Conversation identity

Every agentwire session records, at launch, **which Claude conversation it is**
— plus everything needed to reconstruct that launch. Written to
`~/.agentwire/sessions/<name>/metadata.json` by exactly one function,
`core.record_session_launch`.

Before this, a session's conversation id was unrecoverable except by scraping
tmux scrollback for the resume id Claude prints on `/exit`.

## agentwire mints the UUID

`claude --session-id <uuid>` lets the caller choose the conversation id, so
`build_agent_command` generates one and passes it at launch. The record is
therefore **authoritative**, not a guess reconstructed by watching
`~/.claude/projects/<encoded-cwd>/` for the newest `.jsonl`.

Two verified properties of the flag shape everything else:

- **Collision is fatal.** A reused id fails with `Session ID <id> is already in
  use.` and Claude refuses to start. The check is scoped to the launch cwd
  (that's what keys the history dir), so the same id in a different directory
  is accepted. A fresh `uuid4` per launch is the only safe input — never
  re-pass a recorded id as `--session-id`.
- **Resume composes with it.** `--resume <old> --fork-session --session-id
  <new>` lands the fork at the id *we* chose. That's what makes
  `conversation_ids` a chain rather than a scalar that goes stale the first
  time anyone resumes.

## The record

```jsonc
{
  "created_by": "orchestrator",       // parent for prompt routing (#715)
  "created_via": "worktree",          // which verb created it
  "created_at": "…",                  // first creation; survives relaunch
  "launched_at": "…",                 // THIS launch
  "role": "worker",                   // ROLE axis (#716)

  "conversation_ids": ["…", "…"],     // a CHAIN — --fork-session mints a new id per resume
  "cwd_at_launch": "/Users/…/worktrees/proj/branch",
  "repo": "/Users/…/projects/proj",   // the MAIN checkout, per git
  "branch": "my-feature",
  "worktree_path": "/Users/…/worktrees/proj/branch",  // null when cwd IS the main checkout

  "posture": "bypass",                // enough to REGENERATE the system prompt,
  "roles": ["worker-worktree", "soul"], //   not merely to reference it
  "role_prompt_path": "/Users/…/.agentwire/role-prompts/<conversation-id>.txt"
}
```

The prompt file is written **0600 in a 0700 directory**, matching the posture of
`~/.agentwire/.env`. Both modes are forced rather than requested — `mkdir(mode=)`
and `open(mode=)` are masked by umask and neither touches an already-existing
path, so a directory created before this rule heals on the next write. The
remote mirror sets the same modes on the far side.

Missing keys read as **absent**, never as a default. `repo`/`branch`/
`worktree_path` come from `core.git_identity`, which *asks git* — the same rule
[#837 had to retrofit onto worktree paths](../internals/parallel-refactor.md)
and #868 onto session names. They are all `null` off-repo and for a remote
session (whose path doesn't exist on this machine, and where a same-named local
directory would otherwise answer with some other repo's branch).

## The two failure modes this exists for

They are different in kind, and only the first is fixed here.

**1. The role silently vanishes.** The role prompt used to live in a
`tempfile.NamedTemporaryFile` under `/var/folders`, referenced by the launch
line as `--append-system-prompt "$(<file)"`. macOS garbage-collects that
directory. A session older than the GC window relaunched with an **empty**
system prompt: the conversation came back, the role did not, and nothing
failed loudly — the agent just quietly stopped being a worker.

Fixed by moving the prompt to `~/.agentwire/role-prompts/<conversation-id>.txt`
(`core.ROLE_PROMPTS_DIR`), keyed by conversation so the prompt a conversation
launched with stays recoverable even after its session's roles change. The
remote launch paths mirror the file to the *same* durable location on the
remote (`core.mirror_role_prompt_remote`) — previously only `new` did that, and
only into `/tmp`; `recreate` and `fork` handed the remote a local path, which
is the same empty-prompt bug reached by a different route.

**2. History is orphaned by a moved worktree.** Claude keys conversation
history by cwd (`~/.claude/projects/<encoded-cwd>/`), so relocating a worktree
strands its history and `--resume <id>` fails with `No conversation found with
session ID`. `cwd_at_launch` is what a later check compares against the history
key to **detect** this. Migrating the history is separate follow-up work.

## A recorded id does NOT guarantee a resumable conversation

This is the most important thing to know before building on the record, and it
is easy to assume the opposite.

`conversation_ids` records what agentwire **launched**. It says nothing about
whether Claude still **has** that conversation. The two can diverge:

- A moved worktree orphans the history (above) — the file still exists, under a
  key nothing looks up.
- `~/.claude/projects/` entries disappear on their own. During review of the
  original change, directory count there dropped from 563 to 544 in roughly 25
  minutes with `cleanupPeriodDays` unset. The cause was **not** attributable —
  do not assume it was retention expiry, and do not assume a setting controls
  it. Treat history as a cache that Claude owns and may evict.

The design consequence stands regardless of cause: **"id recorded, history
gone" is a handled state, not an impossible one.** Anything that resumes from
the record must probe for the history file and degrade deliberately — relaunch
fresh with the recorded `roles`/`posture` (which is exactly why those are
recorded to *regenerate* the prompt rather than merely reference it), and say
so, rather than passing `--resume <id>` and surfacing Claude's raw
`No conversation found with session ID`.

Concretely, for the follow-up work: `agentwire restart` must handle it as a
normal branch, and the `doctor` check must distinguish *orphaned* (history
exists under a different cwd key — recoverable by migration) from *gone*
(no history anywhere — not recoverable, relaunch fresh).

## Who writes it

One writer, called exactly once per session launch, right after the launch:

| Path | Verb |
|------|------|
| `session_cli.cmd_new` (local + remote) | `new` — and therefore `worktree`, `orchestrator`, `helper`, and every scheduler/`ensure` dispatch, which all delegate to it |
| `session_cli.cmd_session_recreate` (local + remote) | `recreate` |
| `session_cli.cmd_fork` (worktree, non-worktree, remote) | `fork` |
| `history_cli` resume (local + remote) | `history resume` |
| `system_cli.cmd_dev` | `dev` |

Routing every path through one function is the point: a creation path that
hand-rolls its own record is exactly how the worktree-path (#837) and
session-name (#868) conventions each drifted into a bug that reported success
while doing nothing.

**`spawn` deliberately does not write one.** A worker pane is not a session,
and this store is keyed by session name — a pane recording here would overwrite
its *owning* session's record. Panes still get a minted conversation id and a
durable role prompt from `build_agent_command`; they just have nowhere
session-scoped to put it.

## Design notes

- `AgentCommand` carries the conversation id, role-prompt path, posture and
  role names. The flag builder is the only thing that knows all four, so
  `record_session_launch` takes the whole object rather than loose arguments —
  a caller cannot pair a conversation id with the wrong prompt.
- The write is **merge-preserving**, and `conversation_ids` **appends**.
- `created_by` of `''` means *explicitly rootless* and is written; `None` means
  the caller has no opinion and must not clobber a recorded parent (#848).
- `created_at` is set once and survives relaunch; `launched_at` moves.

## A failed write is loud (#885)

`store_session_metadata` used to end in `except (IOError, TypeError): pass`,
so a failed write was indistinguishable from a successful one. That was
survivable while the record only held `created_by`/`role` — losing it degraded
prompt routing, visibly. It is not survivable now: the record holds the
conversation id, the one piece of session identity that is *not* otherwise
recoverable, which is the exact problem this page exists to solve.

- `store_session_metadata` **raises**. A `TypeError` (unserializable record =
  a code bug) is raised *before* anything is opened, so the bug can never
  truncate a good record on its way out; an `OSError` means the store is not
  writable.
- It writes through `core._atomic_write`, so a crash mid-write leaves the
  previous record intact rather than a truncated file that
  `load_session_metadata` would read back as `{}` via its `JSONDecodeError`
  catch — the same silent loss by a second route.
- `record_session_launch` **catches and warns loudly on stderr** rather than
  propagating. By the time it runs the session is already live in tmux, so a
  traceback would report a failed command for a creation that succeeded. The
  warning names the session, the now-unrecoverable conversation id, and what
  breaks: `history resume`, prompt routing, and the topology view.

## Rooting on remote launches (#886)

Remote records carry `role` and honor an explicit `--created-by`, the same as
local ones — `cmd_new`'s remote branch dropped both until #886, which made the
worktree ↔ conversation ↔ branch mapping local-only. (`recreate` / `fork` /
`history resume` already passed `role` on both sides and have no
`--created-by` flag on either, so there was no asymmetry there to fix.)

What a remote launch deliberately does **not** do is guess a default parent.
The local default runs `resolve_default_created_by`, which inherits the caller
only when the new session's project is the one the caller is already in — and
that comparison reads the caller's *live tmux cwd* against the target path. A
remote target path is on another machine; a same-named local directory would
answer for some other checkout entirely. So the remote default is `None` (no
opinion) rather than `''` (explicitly rootless): the record is keyed by session
*name*, with no machine in it, so writing an explicit rootless marker would
clobber a parent recorded by an earlier launch of the same name. The joint
default with role still applies — an explicitly requested `--kind orchestrator`
roots itself, remote or not.

This is a property of today's transport, not of the relationship: prompt
routing and `notify-parent` are local-only mechanisms (a file inbox drained by
the local watchdog), so a parent link across machines would be a link nothing
traverses. When cross-machine routing exists, the default becomes a real
question again.
