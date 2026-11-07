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

## The flag is single-use; the launch line is not (#901)

The line agentwire builds is stored in the tmux session env as
`AGENTWIRE_LAUNCH_CMD` and **exists to be re-run** — that is the entire point
of [#856/#866](../internals/shell-escaping.md). Putting a single-use
`--session-id` in it made those two facts contradict each other:

```
1. session launches with --session-id <uuid>
2. the agent takes a turn        →  <uuid>.jsonl is created
3. the agent exits               →  /exit, an idle reap, a crash
4. something re-runs the line    →  "Session ID <uuid> is already in use."
5. the pane sits at a bare shell, permanently
```

That stranded 13 live sessions on one machine. The reasoning that let it ship
was "nothing inside agentwire re-evaluates that variable" — true, and beside
the point: the variable is a **public re-entry point**, so "nothing in our code
calls it" is not "it is not called."

The fix is that the flag is chosen **at shell runtime**, against the same
predicate everything else uses (`resumable(id, cwd) == exists(<encoded_cwd>/<id>.jsonl)`):

| on disk | flags |
|---|---|
| no transcript | `--session-id <new>` — first launch stays authoritative |
| `<new>` holds a conversation | `--resume <new>` — re-entry continues it |
| explicit resume, `<old>` holds one, `<new>` doesn't | `--resume <old> --fork-session --session-id <new>` |
| explicit resume, `<old>` gone too | `--session-id <new>` — fresh, role intact, never a bare shell |
| a DEAD id (see below) | no conversation flag at all |

**"The file exists" is not the predicate; "the file holds a turn" is.** Moving
a running session's history directory away leaves a 5-line metadata stub at the
new key — `last-prompt`, `ai-title`, `mode`, `permission-mode`,
`file-history-snapshot` — while the conversation stays under the old one. The
two flags disagree about that file, measured on Claude Code 2.1.222:

```
claude --resume <id>      ->  No conversation found with session ID: <id>
claude --session-id <id>  ->  Error: Session ID <id> is already in use.
```

Neither will take it, so an `[ -f ]` check would have picked one refusal or the
other and left a bare shell either way. The line launches with **no**
conversation flag instead: claude mints its own id and the agent comes up *with
its role*, leaving only the record stale — which `doctor` then reports as a live
session whose recorded conversation has no history. `history.holds_a_conversation`
is the Python twin of that `grep`.

`core._conversation_flags_shell` writes that prelude; the cwd encoding is
mirrored from `history.HISTORY_DIR_SHELL`, which is the shell twin of
`encode_project_path` — **change one, change the other**. Two details there are
measured rather than assumed: `pwd -P`, because Claude keys history by the
*physical* cwd (a symlinked launch dir writes under the resolved path, and on
macOS every `/tmp/...` is a symlink), and an array for the flags, because zsh —
the default login shell here — does not word-split unquoted expansions, so
`claude $flags` would pass one mangled argument.

Testing this needs a **second launch**. A test that evaluates the line once
cannot see the bug, which is exactly how it shipped past a green suite; see
`tests/unit/test_launch_line_reentry.py`, which evaluates the real generated
line twice under both zsh and bash.

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
(`core.role_prompts_dir()`), keyed by conversation so the prompt a conversation
launched with stays recoverable even after its session's roles change. The
remote launch paths mirror the file to the *same* durable location on the
remote (`core.mirror_role_prompt_remote`) — previously only `new` did that, and
only into `/tmp`; `recreate` and `fork` handed the remote a local path, which
is the same empty-prompt bug reached by a different route.

### Retention for that store (#884)

The store grows one file per agent launch, forever, and `spawn` — the
highest-frequency launch path — writes files nothing will *ever* reference
again: a pane gets a minted conversation id and a durable role prompt, but a
pane is not a session and has nowhere session-scoped to record that id.

**"Delete on session exit" would be wrong**, and getting this right is the
whole point of the store. The prompt is durable precisely so it outlives the
process that made it — a session's tmux process dying is not the end of its
conversation; `--resume` brings the conversation back and needs its system
prompt. Exit-deletion reintroduces the `/var/folders` bug above with a tidier
implementation. The lifetime that matters is the **conversation**, not the
session: one conversation chain outlives many kill/recreate cycles.

The rule (`agentwire/role_prompts.py`):

1. **Reachable is forever.** A prompt whose conversation id appears in any
   session's `conversation_ids` chain — or in its recorded `role_prompt_path`
   — is never deleted at any age. An orchestrator conversation running for
   months stays resumable.
2. **Unreachable ages out** after 30 days, by mtime. Weeks, not hours: nothing
   references these, but they must not vanish mid-flight either.
   **This threshold is disk-space policy, not a safety mechanism.** Rule 1 is
   the guardrail and does all the safety work — reachability is checked before
   age is consulted, so a live session cannot be swept at any age, and tuning
   the threshold down must never be able to delete a running agent's prompt.
3. **Panes are the age-out population, deliberately.** The alternative —
   giving panes somewhere to record a conversation id — invents a fourth
   identity axis for the one entity whose entire design is "short-lived,
   reaped by the idle hook, not a session". A pane's prompt is dead the moment
   the pane exits (minutes to hours), so 30 days is two orders of magnitude of
   headroom.

Where it runs: the sweep rides the limits watchdog (which already owns the
periodic housekeeping), self-throttled to once a day via
`~/.agentwire/role-prompt-sweep.json`. `agentwire doctor` reports the store's
size and reachability split, and flags only the **aged-out tail** — because
unreachable-but-young files are the normal steady state, a surviving tail means
the watchdog sweep isn't running. `doctor --yes` sweeps.

Safety, because this is a deletion pass aimed at a directory full of live
agents' system prompts:

- Reachability globs `sessions/**/metadata.json`, **recursively**. Session
  names contain slashes by design — `tmux_safe_name` rewrites only `.` and `:`,
  and `project/branch` is what every `agentwire worktree` and every scheduler
  dispatch is called — so those records nest one level deeper than a flat glob
  looks. A flat `sessions/*/metadata.json` found 469 of 1106 records in the
  wild: 58% of live conversations reading as unreachable, with age the only
  thing left protecting them. This is why rule 1, not rule 2, is the guardrail.
- `sweep()` takes its store and its reachability source as **required
  parameters**. Only `tick()` resolves the real ones.
- `core.role_prompts_dir()` is a **function**, not the import-time constant it
  used to be. A constant does not follow this repo's isolation seam
  (`monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)`), so a test
  that believed it had isolated the config dir would still have pointed a
  deletion pass at the operator's real store.
- Only regular files named `<uuid4>.txt` directly inside the store are ever
  unlinked — directories, symlinks and any other filename are reported and
  left alone, so even a misaimed sweep can't delete a stranger's data.
- Nothing copies a prompt and nothing widens a mode. The files stay 0600 in a
  0700 dir; a GC that leaked a world-readable copy would undo that fix.

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

Concretely: `agentwire restart` handles it as a normal branch, and the `doctor`
check distinguishes *orphaned* (history exists under a different cwd key —
recoverable by migration) from *gone* (no history anywhere — not recoverable,
relaunch fresh).

### One predicate

Both live and dead states reduce to a single question, `history.
locate_conversation` is the one place that asks it, and `restart` and `doctor`
both call it so they can never disagree:

```
resumable(id, cwd) == exists(<encoded_cwd>/<id>.jsonl)
```

That one file governs both directions of the flag pair. `--resume` finds the
conversation iff the file is there; `--session-id` rejects an id as "already in
use" iff the file is there. Re-passing the id of a session that never took a
turn is *accepted*, because nothing was ever written — the transcript is
created lazily, on the first turn.

The cwd key comes from `history.encode_project_path`, which collapses **every**
non-alphanumeric character to `-`, not just the separators: `/Users/dotdev/.claude`
is stored as `-Users-dotdev--claude`, and `enc b.c_d` as `enc-b-c-d`. Measured
against the installed Claude Code, the same way #878 established tmux's
mapping. Encoding only `/` (#892) produced a key no directory ever had for any
path with a dot, underscore or space.

## The chain follows the conversation, not just the launch (#1271)

`record_session_launch` writes the chain **once**. Claude mints a new id inside
a running session on `/clear` and switches ids on `/resume`, so a chain that
only knew launches resumed a week-old conversation after six `/clear`s — the
record named the id the session *started* with, not the one it was *in*. A
single-launch test cannot see this; the bug only exists after a second step.

A Claude Code `SessionStart` hook (`source`: `startup|resume|clear|compact`,
carrying the new `session_id`) reports each change:

- `hooks/session-start-handler.sh` is registered by `agentwire hooks install`
  like the other managed hooks (drift checks in `hooks status` / `doctor`
  included). It runs the work detached and exits 0 unconditionally — a hook
  must never fail or slow a session start.
- The body is `agentwire hooks session-start` (CLI is SSOT). It resolves the
  owning session from `$TMUX_PANE` via `pane_manager`, **skips worker panes
  (index > 0)** — a pane is not a session and must not write its owner's
  chain, same rule as `spawn` — and calls `core.record_conversation_change`,
  which uses the same atomic `store_session_metadata` write.
- `record_conversation_change` puts the id **last** in the chain (an id already
  present — `/resume` back to an earlier conversation — moves to the tail,
  because the tail is what restart reads as "current"). Already-last is a no-op,
  so it is idempotent and the launch's own id costs nothing. A session with no
  launch record is left alone: the hook never conjures a partial record.
- **Only the session's own conversations count (#1289).** A headless
  `claude -p` run from a pane (a worker's comparison or spike in a scratch dir)
  inherits `$TMUX_PANE` and fires the same hook. Verified payload shape:
  `{session_id, transcript_path, cwd, hook_event_name, source}` — `cwd` is the
  physical path, and a headless run reports `source: "startup"`, identical to a
  real start, so **`source` is no signal**; `cwd` is. The hook records an id
  only when `core.session_owns_cwd` says the payload `cwd` (through
  `history.encode_project_path` of the realpath) equals the recorded
  `cwd_at_launch` or `worktree_path`. A mismatch, or a payload with no `cwd`,
  is ignored and the reason goes to the debug log. Chains polluted before this
  fix keep their stray ids (`restart` skips ids with no transcript under the
  launch cwd, so they are inert there); `doctor`'s orphaned-history output
  names nested runs as the likely cause.
- Errors are logged to `/tmp/claude-hook-debug.log`, never raised.

`restart`'s newest-first walk is unchanged: a `/clear`ed conversation that
never took a turn has no transcript yet (written lazily), so the walk falls
back to the previous id that does.

**Sessions that took a `/clear` before the hook was installed** have a chain
frozen at launch. `doctor` states (never scores) live sessions whose history
dir holds transcripts newer than the recorded head and absent from the chain.
It cannot be certain — another session in the same directory writes transcripts
that look identical — hence a note, not a fault.

## Restarting in place

`agentwire restart -s <session>` is the verb the record exists for: `/exit`,
regenerate the launch flags, relaunch at the same cwd with `--resume`. The
alternatives all cost something — `recreate` `rm -rf`s the worktree and cuts a
new branch, `history resume` forks into a *new* tmux session, `kill` + `new`
drops the conversation entirely. It also works on a session that isn't
currently running, which is the post-reboot case the epic opens with.

**Regenerate, never re-evaluate.** The flags are rebuilt from the recorded
`roles` / `posture` / `model` through `build_agent_command`. The previous
launch line is still sitting in the tmux session env as `AGENTWIRE_LAUNCH_CMD`,
and re-`eval`ing it is the one thing that must not happen: it carries a
single-use `--session-id`, so the relaunch dies with "already in use" and drops
the pane to exactly the bare shell `_guarded_launch_command` exists to prevent.
The recorded id is only ever passed to `--resume`.

**The chain is walked newest-first**, not read from the tail. A session that
was relaunched and then never spoken to has no transcript for its newest id at
all, while the id it forked *from* still holds the whole conversation — taking
only the tail would throw that away and start blank.

**Degradation is a normal branch, and it is stated.** When nothing in the chain
resolves, restart launches fresh with the role intact and prints why —
`orphaned` names the key the history actually sits under, `gone` says it was
either never prompted or evicted. It never passes `--resume` at a conversation
it hasn't found, so Claude's raw `No conversation found with session ID` is
never what the operator sees.

Refusals, all up front: a remote session (the probe is local), a recorded cwd
that no longer exists, no launch record at all, and **restarting itself** —
`/exit` + `kill-session` would take down the tmux session the command is
running in, leaving nothing to relaunch. A missing *role* file warns instead of
refusing: a partial role beats stranding the session with no way back, but the
relaunched agent's etiquette differs from what it was launched with, so it is
said loudly.

Restart appends to the record rather than rewriting it: `created_by`,
`created_via` and `role` survive untouched, because a restart is not a
creation.

## Detecting orphaned history

`agentwire doctor` reports sessions whose conversation is intact but keyed to a
directory they no longer run in. The key it compares against is where the
session **runs** — its live pane cwd (`core.tmux_session_cwd`) when it's up,
else `cwd_at_launch` — which is what catches the case where the record and the
transcript still agree with each other and are both stale because the directory
moved underneath them.

It scores orphans of **live** sessions only (#1292). A session that is not
running keeps its record under `~/.agentwire/sessions/` after
`worktree --remove`, so its orphaned chain would otherwise stay red forever;
doctor lists those as information with the recover command and does not count
them. Teardown (`worktree --remove`, `--prune --gc-merged`) never deletes the
record — it is the only map from session name to conversation — but says in its
output that it left one, and how many of its chain's conversations are
orphaned. To clean up deliberately:

```bash
agentwire history prune --dead                    # list dead-session records (deletes nothing)
agentwire history prune --dead --older-than 14d   # only records untouched that long
agentwire history prune --dead --yes              # delete exactly what was listed
```

`doctor`, teardown and `prune` share one chain probe, `history.survey_chain`.

Orphans aside, a conversation with no history *anywhere* is stated for
live sessions and never counted, because this check cannot say why it's missing
— never-prompted and Claude-evicted look identical from here — and because
scoring it would flag hundreds of dead records. Calibration, measured on a real
store while building it: 466 session records, 7 carrying `conversation_ids`, 28
recorded ids with no transcript, **0** genuinely orphaned — and all 28 belonged
to a single record the test suite had polluted (#893).

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

## The cwd → history-directory encoding

Claude Code keys a transcript by the directory it ran in:
`~/.claude/projects/<encoded-cwd>/<conversation-id>.jsonl`. The encoding is
**per character: everything outside `[A-Za-z0-9]` becomes `-`.** Nothing is
dropped, run-collapsed, or case-folded.

This was derived empirically (#871/#892), the same way #878 had to measure
tmux's name mangling rather than assume it — twice, independently, by two
sessions that agreed:

- Every `*.jsonl` records the `cwd` it was written from, giving ground-truth
  pairs straight off disk. 528 and 533 pairs were checked; the rule fits with
  no mismatches.
- The remaining characters were swept through real `claude` runs. A directory
  segment `a_b.c+d~e@f,g=h!i#j%k^l&m n o'p` yields
  `a-b-c-d-e-f-g-h-i-j-k-l-m-n-o-p`, and `café-日本-Ωx` yields `caf------x` —
  so the class is ASCII `[A-Za-z0-9]`, **not** `str.isalnum()`, which would
  have preserved `é`/`日`/`Ω`.

`history.encode_project_path` is the one implementation. It previously replaced
only `/`, which silently produced a non-existent directory for any path holding
a dot, underscore or space — including `~/.claude` and
`~/.agentwire/council/<n>/workspace`. The lookup then found nothing and
reported nothing, the same dot-shaped bug class as #865 → #868 → #870 → #878.

**There is no inverse, and `decode_project_path` was deleted rather than
fixed.** The mapping is many-to-one — `/`, `.`, `_` and `-` all encode to `-` —
so a directory name cannot be decoded back to a cwd. It can only be compared
against the encoding of a cwd you already know, which is what `cwd_at_launch`
is for. The old round-trip test passed only by choosing paths that dodged the
ambiguity, pinning the bug as intended behaviour.

A consequence worth naming: `/p/a_b` and `/p/a.b` are distinct directories that
**share one history directory**. That is a property of Claude Code, not
something agentwire can repair, and it is why a migration destination may
already hold an unrelated project's transcripts.

### Is a conversation resumable?

One predicate, used everywhere rather than re-invented per caller:

```
resumable(id, cwd) == exists(<encoded-cwd>/<id>.jsonl)
```

The same file governs both directions. A launched-but-never-prompted session
has **no transcript at all** — the `.jsonl` is written lazily on the first turn
— so a recorded conversation id can be entirely valid and still not resumable.
`--session-id` likewise reports a collision on that file *existing*, not on the
id having been used before. A recorded id is therefore never a promise that
`--resume` will work.

## Repairing history orphaned by a moved directory

Move a worktree and its transcripts stay behind under the old key, so
`--resume` fails with *"No conversation found with session ID"* while the file
sits intact on disk. `agentwire history migrate` re-keys it.

```bash
agentwire history migrate --all                 # dry run: what's orphaned
agentwire history migrate -s <session>          # reconcile one session
agentwire history migrate --from OLD --to NEW   # a move agentwire never saw
agentwire history migrate ... --apply           # perform it
```

**Why a `history migrate` verb and not `worktree --move`.** #871 originally
asked for the latter, describing a flag that does not exist. A move verb would
only repair moves made *through agentwire*, and that is the minority of them —
`git worktree move`, a plain `mv`, and a reorganised `~/worktrees` orphan
history identically and would all still be broken. The damage is not caused by
moving; it is caused by the recorded cwd and the real cwd disagreeing, which
`cwd_at_launch` makes detectable. Keying the repair on that disagreement makes
it work no matter who moved the directory, and keeps it composable: the same
`history_migrate.scan()` that powers the dry run is what a doctor orphan check
consumes.

**Two guarantees.**

1. *History is never destroyed.* Every migration copies into a staging
   directory, fingerprints the copy against the source (size + sha256 per
   entry, symlinks by target), and only then publishes it with a single
   rename. The source is retained unless `--prune-source` is passed, and even
   then only after verification passed. An interrupted run leaves the source
   untouched and the target absent.
2. *A populated destination is refused, never merged.* Because the encoding is
   non-injective, the target may hold an **unrelated** project's transcripts,
   so merging would silently interleave two projects' history. The check runs
   at plan time and again immediately before the rename, closing the window
   where a concurrent `claude` run creates the target mid-copy. Note that
   `shutil.move` onto an existing directory does *not* fail — POSIX nests the
   source inside it as `dst/<basename>`, burying transcripts one level below
   where Claude Code looks while the command reports success. That is the #868
   failure shape, and it is why publishing goes through `os.rename` behind an
   explicit existence check.

**3. A mixed-provenance source migrates selectively.** One history directory
can hold transcripts from *two* projects — a directory renamed under a live
session leaves the old key holding both. One such directory exists on the
machine this was written on: 7 transcripts from one project and 6 from
another. Relocating all 13 would orphan the 6, breaking guarantee 1 while
reporting success.

Rather than refuse (safe, but leaves no way forward except moving files by
hand), the migration moves only the transcripts whose own recorded `cwd`
matches — the split is exactly computable from the same ground truth the
encoder came from. Foreign transcripts are left where they are, which leaves
them no worse off than before, whereas moving them would strand them
somewhere new. Files with no readable `cwd`, and non-transcript entries like
`memory/`, travel with the migration: they carry no evidence of belonging
elsewhere. `--prune-source` **refuses to prune** a source that still holds
foreign transcripts — otherwise it would delete precisely what was
deliberately left behind, turning the safe choice into the destructive one.

Missing source history is a **normal outcome** (`source_absent`), not an error:
transcripts have been observed disappearing on their own, and a never-prompted
session never had one. A sweep reports sessions it cannot judge as a counted
summary rather than a wall of lines — counted, never silently dropped.

An interrupted run can leave a `.agentwire-migrate-<hex>` staging directory
behind; `apply` sweeps them before starting. A staging directory only ever
holds a *copy* — the source is untouched until after publication — so clearing
one cannot lose history.

## Design notes moved from CLAUDE.md (#1280)

Verbatim from the repo-root CLAUDE.md, which now keeps only the rule and a pointer.

**Conversation identity is RECORDED, not reconstructed (#871).** Same SSOT shape, third axis. agentwire MINTS the conversation UUID in `build_agent_command` and passes `claude --session-id <uuid>`, so `~/.agentwire/sessions/<name>/metadata.json` is authoritative instead of a guess scraped from tmux scrollback or from whichever `~/.claude/projects/<encoded-cwd>/*.jsonl` was newest. `core.record_session_launch()` is the ONE launch writer and `core.load_session_metadata()` the one reader; the chain also follows in-session id changes (`/clear`, `/resume`) via a SessionStart hook (`hooks/session-start-handler.sh` → `agentwire hooks session-start` → `core.record_conversation_change`, pane 0 only, same atomic write, moves the id to the chain tail, always exits 0 — #1271); every path that launches a session in tmux calls it exactly once (`new` — hence `worktree`/`orchestrator`/`helper`/scheduler dispatch — plus `recreate`, `fork`, `history resume`, `dev`). `spawn` deliberately does not: a pane is not a session, and this store is keyed by session name, so a pane writing here would overwrite its OWNING session's record. Two flag properties are load-bearing and verified, not assumed: `--session-id` **hard-errors on collision** ("already in use", scoped to the launch cwd) so you must mint a fresh uuid4 and never re-pass a recorded id; and `--resume <old> --fork-session --session-id <new>` composes, which is why `conversation_ids` is a **chain** — `--fork-session` mints a new id on every resume. The flag is **single-use, and the stored launch line is not** (#901): `AGENTWIRE_LAUNCH_CMD` exists to be re-`eval`'d (#856/#866), so a line carrying a fixed `--session-id` died with "already in use" the moment the session had taken one turn and exited — 13 live sessions stranded at a bare shell. The flags are therefore resolved **by the shell at launch** (`core._conversation_flags_shell`): no transcript → `--session-id <new>`, transcript present → `--resume <new>`, explicit resume → the fork pair, and a vanished `<old>` degrades to fresh-with-role rather than to a bare shell. The cwd encoding is mirrored in shell as `history.HISTORY_DIR_SHELL` (`pwd -P`, because Claude keys by the PHYSICAL cwd) — change it and `encode_project_path` together. Test it with a SECOND launch; a single-launch test cannot see this. `repo`/`branch`/`worktree_path` come from `core.git_identity()`, which asks git; absent keys mean unknown, never a default. The write itself is **loud** (#885): `store_session_metadata` raises (atomically — a failed write can't truncate a good record) and `record_session_launch` warns on stderr instead of propagating, since the session is already live by then. Remote launches record `role` and honor an explicit `--created-by`, but never GUESS a parent — same-project inheritance reads the caller's live tmux cwd, which can't speak for a path on another machine (#886). The role prompt now lives at `~/.agentwire/role-prompts/<conversation-id>.txt` (`core.role_prompts_dir()` — a FUNCTION, so it follows a patched `CONFIG_DIR`; as an import-time constant it silently ignored the test seam, which is a data-destruction trap once anything sweeps that dir), never `/var/folders` — macOS GC'd the old temp file and, since the launch line reads it BY PATH (`--append-system-prompt "$(<file)"`), a session older than the GC window relaunched with an **empty** system prompt: conversation intact, role silently gone. Remote launches mirror it to the same durable path via `core.mirror_role_prompt_remote()`. Detail: [`docs/wiki/sessions/conversation-identity.md`](conversation-identity.md).

**`agentwire restart -s <session>` is what the record is FOR.** `/exit`, REGENERATE the flags from the recorded roles/posture/model via `build_agent_command`, relaunch at the same cwd with `--resume` — same conversation, same tmux session, nothing on disk touched (`recreate` force-removes the worktree and recursively deletes the directory; `history resume` forks into a NEW session). Works on a session that isn't running (the post-reboot case). It **regenerates rather than replays** the stored `AGENTWIRE_LAUNCH_CMD`: the flags come from the record, so a role/posture/model change takes effect on relaunch, and restart stays independent of whatever an older session is carrying in its tmux env (#901 separately made that stored line safe to re-run, which is a different guarantee). A **recorded id does not guarantee a resumable conversation**: everything reduces to one predicate, `resumable(id, cwd) == exists(<encoded_cwd>/<id>.jsonl)` — `history.locate_conversation`, the ONE probe `restart`, `doctor` and `history migrate` share. The transcript is created LAZILY on the first turn, and `~/.claude/projects/` entries were observed vanishing (563→544 in ~25min, cause undetermined) — so restart walks the chain newest-first, and when nothing resolves it starts FRESH with the role intact **and says so**, distinguishing *orphaned* (exists under another cwd key — a moved directory; recoverable via `history migrate`) from *gone*. `doctor` **surveys the whole chain** rather than stopping at the first resumable id — restart's question is "newest resumable?" (first match wins), doctor's is "is ANY link orphaned?", and collapsing the two made restart silence the warning one turn later. It reports orphans (scored) and live sessions with no history at all (stated, never scored — it can't say why, and it would flag hundreds of dead records).

## Teardown drops the record only when nothing is left to resume (#1327)

The record is the only map from session name to conversation, so teardown
(`worktree --remove`, `--prune --gc-merged`, MCP `worktree_remove`) decides
per teardown instead of leaving every record for a manual
`history prune --dead --yes`. One rule, `session_cli.record_is_finished`:

| teardown | record |
|---|---|
| session owned (not a `pane` entry) **and** worktree removed **and** branch merged-and-deleted | **dropped** — the worker is finished and its work is integrated |
| `--keep-branch`, unmerged branch kept, or a pane entry (the session is its orchestrator's) | **kept** — `restart -s` still resumes it |

The branch-deleted leg is the integration signal: `_delete_branch_if_safe`
only deletes after verifying a merge (or an explicit force). The deleter is
`core.delete_session_record`, beside the one writer and one reader; `history
prune --dead` uses the same function. The teardown message states which
happened either way ("session record dropped …" / "session record kept at …").
`agentwire kill` is unchanged. Pinned by `TestTeardownRecordRule` in
`tests/unit/test_worktree_git_truth.py` (mutation-checked: `return True` turns
three of the four red).
