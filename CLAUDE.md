# AgentWire

Voice interface for AI coding agents. Push-to-talk from any device to tmux sessions running Claude Code.

**No Backwards Compatibility** - Pre-launch, no customers. Change things completely, no legacy fallbacks.

## Dev Workflow

`uv tool install` caches builds and ignores source changes.

```bash
# Step 0 — after a remote merge, pull first. rebuild reinstalls whatever is
# checked out, so a never-pulled main silently ships stale code.
git pull --ff-only

# During development (picks up changes instantly)
agentwire portal start --dev

# After structural changes (pyproject.toml, new files)
agentwire rebuild

# After code changes: ALWAYS do both
git pull --ff-only && agentwire rebuild && agentwire portal restart --dev
```

Rebuild alone = stale static files. Restart alone = stale Python. The MCP server runs as a separate process started by Claude Code — session restart required after rebuild to pick up MCP changes. `rebuild` now refuses (warn + `--force` to override) when the checkout is behind `origin/main`, and `agentwire doctor` flags a behind-main checkout, a disabled kill switch (`enabled: false` in `~/.agentwire/damagecontrol.yml`), damage-control rule/hook/matcher drift, and any of `~/.agentwire/` · `.env` · `portal.token` · `machines.json` readable beyond its owner (`doctor --yes` tightens them; #887).

## CLI is the Single Source of Truth

All session/machine logic lives in CLI commands (`agentwire/__main__.py`). The portal (`agentwire/server.py`) is a thin wrapper that:

1. Calls CLI via `run_agentwire_cmd(["command", "args"])`
2. Parses JSON output (`--json` flag)
3. Adds WebSocket/real-time features

**When adding new functionality:**
1. Implement in CLI first with `--json` output
2. Portal calls CLI, doesn't duplicate logic
3. Never bypass CLI with direct tmux/subprocess calls

**CLI layout (#495):** the old `__main__.py` monolith is split per-domain. Shared
helpers (machine config, SSH `_run_remote`, JSON output, session resolution, etc.)
live in `agentwire/core.py`; each command group lives in its own `agentwire/<domain>_cli.py`
module exposing a `register_<domain>_parser(subparsers)` registrar. `build_parser()`
imports them and runs the `_REGISTRARS` loop — adding a command means writing a new
`*_cli.py` + appending its registrar, not editing a god-file. Example: the portal git
endpoints (`api_check_path` / `api_check_branches`) shell out to `agentwire repo-info`
and `agentwire branches` (in `repo_cli.py`) rather than embedding their own git/SSH logic.

Full CLI command reference lives in the `agentwire-cli` skill.

## MCP Server (For Agents)

**Agents running in agentwire sessions should use MCP tools instead of CLI commands.**

The `agentwire-mcp-tools` skill has the full reference (sessions, panes, voice, tasks, outbound channels, scheduler, desktop UI, handoffs). Rule of thumb: MCP for agents, CLI for humans/scripts.

**Three independent axes (#716):** every session has a ROLE, a TOPOLOGY, and a ROOTING — none inferable from the others.

- **ROLE** ∈ {`orchestrator`, `worker`, `reviewer`} — authority + etiquette. Orchestrator = durable window, reviews + merges, directs children (a replaceable persona — explicit `--roles` swaps it out cleanly). Worker = scoped task, report-back, draft-PR-don't-merge (a non-overridable safety rail — `--roles` STACKS on top, never replaces it). Reviewer = worker's rail inverted (#827) — adversarially reviews a sibling's PR, never opens/merges its own, reports a verdict via `notify_parent`; also non-overridable, parented like worker (not rooted). Says nothing about location.
- **TOPOLOGY** ∈ {`main`, `worktree`, `pane`} — where it runs. A worker's concrete etiquette differs by topology (a worktree worker pushes a branch and opens a draft PR, keeps voice, and can ask via prompt-routing; a pane/main-topology worker is headless, writes an exit-summary, and gets auto-killed) — but the ROLE name itself (`worker`) is the same either way.
- **ROOTING** ∈ {root, parented} (#715) — `created_by`, drives prompt routing + notify-parent.

**Note:** MCP tools don't support git worktree creation. For worktree-isolated work, use the CLI directly — and pick the right primitive:

| Term | Command | What you get |
|------|---------|--------------|
| **Worktree session** | `agentwire worktree <name> -p <repo>` | **Standalone tmux session** named `{project}-<name>`, new branch `<name>` from origin/main, worktree at `~/worktrees/<project>/<name>/` (nested per project, mirroring `~/projects/`). Survives independently; report-back via `agentwire notify-parent --to <orchestrator>`. Role defaults to `worker` (zero behavior change) — `--kind orchestrator` makes it a durable, replaceable-persona project window instead of a subordinate (see below). Etiquette for the default role (isolation, no rebuild/restart, verify in-worktree, draft PR + notify-back) is intrinsic — the `worker-worktree` role is auto-injected by the verb, so first prompts only need the task. |
| **Worker pane** | `agentwire spawn --branch <name>` | A **pane inside the current session** (pane 1+), worktree on `<name>`. Inherits the session's dashboard; idle hook reaps it. |

When the owner says "worktree session", they mean the standalone session (`agentwire worktree`), **never** `spawn` panes. Worker panes are for small subtasks watched by the orchestrator; worktree sessions are for parallel autonomous work. Every spawn defaults to the **bypass** posture — workers included; damage-control hooks are the guard, not tool-locking — so no `--posture` is needed for the common case.

**`agentwire orchestrator [name] -p <project>`** is sugar for `worktree --kind orchestrator` — the durable-window one-liner: worktree topology + orchestrator role + rooted by default (a durable orchestrator shouldn't inherit whoever spawned it — the joint default below). `name` defaults to `"orchestrator"` if omitted.

**Rooting (#715 + #716):** `agentwire worktree`/`new` record the calling session as the new session's parent (`created_by`, drives prompt routing + notify-parent) **only when the target repo is the caller's own project** — spawning into a genuinely different project defaults to a standalone root instead of nesting under the caller. Same-project fan-out (a worktree session spawning another worktree of its own project) still parents as before. `--created-by <name>` forces a specific parent regardless of project; `--created-by ''` forces standalone even within the same project. **Joint default with role:** an explicit `--kind orchestrator` (on `new` or `worktree`, e.g. via the `orchestrator` sugar verb) also defaults `created_by` to `''` (root) unless `--created-by` says otherwise — a durable orchestrator shouldn't answer to whoever happened to spawn it. The default-derived orchestrator (a plain branchless name, no `--kind` given) keeps the ordinary same-project-inherit behavior above unchanged. Detail: [`docs/wiki/sessions/prompt-routing.md`](docs/wiki/sessions/prompt-routing.md).

**Dangling-PR detection (#716):** `agentwire worktree --dangling` (and `agentwire doctor`) flag LIVE worker sessions with an OPEN PR and no live recorded parent — the concrete failure mode a rooted-but-still-subordinate session hits: it correctly refuses to self-merge, so the PR just dangles with nothing positioned to act on it. Distinct from `--list`'s "orphan" state (a dead session whose worktree dir is left on disk).

**Worktree paths come from git, not a convention (#837 + #855).** One helper creates+registers (`worktree.create_and_register_worktree`) and one resolves a session's real worktree by ASKING GIT (`worktree.find_git_worktree` / `git worktree list --porcelain`). All five creation sites route through the first — `spawn --branch`, `new -s project/branch` (what every scheduler dispatch shells out to), `worktree`, `recreate`, `fork` — so nothing is invisible to `--list`/`--dangling`/`--prune`/`--remove` anymore. `--remove`/`--status` route through the second: the documented layouts (`~/worktrees/<project>/<name>/` and `~/projects/<project>-worktrees/<name>/`, both live in the wild) are a *default*, never a guarantee, so string-building a path and acting on it is how a teardown reports success while removing nothing. When nothing real resolves, `--remove` now FAILS loudly and lists what git does know, rather than tearing down a guess. Registry entries carry `topology` — `pane` entries (from `spawn --branch`) name the OWNING session, so teardown never kills it and `--dangling` skips them. Never write `~/worktrees/<project>/<name>` in new code; call the helpers.

**Session NAMES come from the same SSOT (#868, #878).** The path axis isn't the only one a convention can lie about. tmux treats `.` and `:` as its address separators (`session.window`, `session:window`) and silently rewrites **both** to `_`, so a project dir with a dot (`~/.claude`, `dotdev.dev`) gets a session named `_claude-<name>`, not `.claude-<name>` — and `-s proj:x` gets you `proj_x`. `worktree.tmux_safe_name()` is the one implementation of that mapping and `worktree.worktree_session_name()` applies it; never inline the substitution or build `f"{project}-{name}"` by hand. Those two are the COMPLETE 1:1 substitution set, measured by sweeping every printable ASCII char through real tmux (#878) — don't widen it by guess. (tmux also vis-escapes `\` and control chars, but that escaping isn't idempotent, so such names have no fixed point and are deliberately left alone.) Resolution prefers reality over the registry (a live pane's cwd names its own session) and re-sanitizes a recorded name, so entries written before the fix heal on read — no data migration. And teardown states the session's fate *explicitly* in all three cases via `worktree.teardown_session_note()` — killed / deliberately left alone (`pane` topology) / **no live session matched**. That last one rendering as silence is what turned a name mismatch into a session leaked into a deleted directory under a line claiming it was removed.

**Conversation identity is RECORDED, not reconstructed (#871).** Same SSOT shape, third axis. agentwire MINTS the conversation UUID in `build_agent_command` and passes `claude --session-id <uuid>`, so `~/.agentwire/sessions/<name>/metadata.json` is authoritative instead of a guess scraped from tmux scrollback or from whichever `~/.claude/projects/<encoded-cwd>/*.jsonl` was newest. `core.record_session_launch()` is the ONE writer and `core.load_session_metadata()` the one reader; every path that launches a session in tmux calls it exactly once (`new` — hence `worktree`/`orchestrator`/`helper`/scheduler dispatch — plus `recreate`, `fork`, `history resume`, `dev`). `spawn` deliberately does not: a pane is not a session, and this store is keyed by session name, so a pane writing here would overwrite its OWNING session's record. Two flag properties are load-bearing and verified, not assumed: `--session-id` **hard-errors on collision** ("already in use", scoped to the launch cwd) so you must mint a fresh uuid4 and never re-pass a recorded id; and `--resume <old> --fork-session --session-id <new>` composes, which is why `conversation_ids` is a **chain** — `--fork-session` mints a new id on every resume. The flag is **single-use, and the stored launch line is not** (#901): `AGENTWIRE_LAUNCH_CMD` exists to be re-`eval`'d (#856/#866), so a line carrying a fixed `--session-id` died with "already in use" the moment the session had taken one turn and exited — 13 live sessions stranded at a bare shell. The flags are therefore resolved **by the shell at launch** (`core._conversation_flags_shell`): no transcript → `--session-id <new>`, transcript present → `--resume <new>`, explicit resume → the fork pair, and a vanished `<old>` degrades to fresh-with-role rather than to a bare shell. The cwd encoding is mirrored in shell as `history.HISTORY_DIR_SHELL` (`pwd -P`, because Claude keys by the PHYSICAL cwd) — change it and `encode_project_path` together. Test it with a SECOND launch; a single-launch test cannot see this. `repo`/`branch`/`worktree_path` come from `core.git_identity()`, which asks git; absent keys mean unknown, never a default. The write itself is **loud** (#885): `store_session_metadata` raises (atomically — a failed write can't truncate a good record) and `record_session_launch` warns on stderr instead of propagating, since the session is already live by then. Remote launches record `role` and honor an explicit `--created-by`, but never GUESS a parent — same-project inheritance reads the caller's live tmux cwd, which can't speak for a path on another machine (#886). The role prompt now lives at `~/.agentwire/role-prompts/<conversation-id>.txt` (`core.ROLE_PROMPTS_DIR`), never `/var/folders` — macOS GC'd the old temp file and, since the launch line reads it BY PATH (`--append-system-prompt "$(<file)"`), a session older than the GC window relaunched with an **empty** system prompt: conversation intact, role silently gone. Remote launches mirror it to the same durable path via `core.mirror_role_prompt_remote()`. Detail: [`docs/wiki/sessions/conversation-identity.md`](docs/wiki/sessions/conversation-identity.md).

**Teardown ordering:** before tearing down a worker's worktree session, verify its PR is actually merged (the issue is CLOSED, not just the PR shown green) — never teardown-then-check. `agentwire worktree --remove` already refuses to delete a branch that isn't confirmed merged; `--force-delete-branch` overrides that for plain unmerged/local-only work, but it does **not** bypass an OPEN PR on the branch (#756) — deleting the remote head branch of an open PR silently closes it. That guard fires only on the force path (an unmerged branch with no PR, or a merged/closed one, is unaffected) and refuses by naming the PR number; the explicit escape hatch is `--close-pr-branch`, only for when you actually mean to close that PR. `gh` absent/no GitHub remote → best-effort, proceeds like today. Same guard on `--prune --gc-merged` (shared `_teardown_entry` → `_delete_branch_if_safe`). Recovering a branch whose PR got closed this way: `git fetch origin refs/pull/<N>/head` → `git push origin <sha>:refs/heads/<branch>` → `gh pr reopen <N>`.

## Config Layout (`~/.agentwire/`)

| File | Purpose |
|------|---------|
| `config.yaml` | Main config (see `agentwire-config` skill) |
| `.env` | **All API keys/secrets** — the one blessed spot, loaded on every entry point. `chmod 600`. See [`docs/wiki/security/secrets.md`](docs/wiki/security/secrets.md) |
| `machines.json` | Remote machines registry |
| `scripts/` | Machine-specific helper scripts (TTS, startup, service wrappers). Local only, not version controlled. `~/bin/` entries should symlink here. |
| `voices/` | Custom TTS voice samples |
| `uploads/` | Uploaded images for cross-machine sharing |
| `artifacts/` | Agent-generated HTML for artifact windows |
| `wiki/` | LLM-maintained knowledge base (Karpathy LLM Wiki pattern) |
| `logs/` | Audit logs for damage-control |

Per-project config lives in `.agentwire.yml` at the project root — **keep it gitignored** (personal config; a tracked copy makes worktree-dispatched runs silently use the stale committed version). `.agentwire.yml` is purely declarative (type/roles/voice/parent/worktree) and agent-writable; named tasks (pre/post/on_task_end/shell) live in the separate, protected sibling `.agentwire.tasks.yml`, authored via `agentwire tasks review`/`promote` (#720). See `agentwire-project-config` skill for fields and task schema.

## Key Patterns

- **agentwire sessions** coordinate via voice, delegate to workers
- **worker panes** spawn within the orchestrator's session (visible dashboard)
- **Pane 0** = orchestrator, **panes 1+** = workers
- **Damage-control hooks** block dangerous ops (`rm -rf`, `git push --force`, etc.)
- **Unattended guardrail** — scheduler dispatches are marked `AGENTWIRE_UNATTENDED=1`; the hook resolves `ask`-tier commands by failing closed (block + email owner) unless on the `unattended_allow` list. See [`docs/wiki/internals/damage-control.md`](docs/wiki/internals/damage-control.md).
- **Smart TTS routing** — audio goes to browser if connected, local speakers if not

### Worker Pane Lifecycle

Workers auto-kill after sending idle notification. The idle hook captures output, sends alert to pane 0, then kills the worker. Manual kill if needed: `agentwire kill --pane 1`.

## Hook Installation

One command installs/refreshes everything agentwire-owned:

```bash
agentwire hooks install   # permission hook, idle handler, queue processor, slash commands
agentwire doctor          # verify (flags stale copies, not just missing ones)
```

Installs (symlinks by default, `--copy` to copy):

| File | Target | Purpose |
|------|--------|---------|
| `agentwire-permission.sh` | `~/.claude/hooks/` | Permission dialogs in portal (registered as `PermissionRequest` hook) |
| `idle-handler.sh` | `~/.claude/hooks/` | Worker notifications + scheduled task completion (registered as `Notification` hook) |
| `queue-processor.sh` | `~/.agentwire/` | Sends queued alerts with 15-second gaps to avoid overwhelming orchestrators |

These files are agentwire-owned: `hooks install` replaces any copy that drifts from the packaged source. Re-run it after `agentwire rebuild` to pick up hook changes.

### Diagnosing Issues

```bash
agentwire doctor                        # all components
tail -f /tmp/claude-hook-debug.log      # hook debug
tail -f /tmp/queue-processor-debug.log  # queue processor
```

## Wiki (Knowledge Base)

LLM-maintained knowledge base at `~/.agentwire/wiki/` using the Karpathy LLM Wiki pattern. Research and debugging knowledge compounds across sessions. **Authoring is in-context** — the session that learns something writes the page itself (there's no batch ingester); the *mechanical* ops are deterministic, via the `agentwire wiki` CLI and the `wiki_query` / `wiki_lint` / `wiki_status` MCP tools (`status` / `query` / `lint` / `new` / `done` — stdlib-only, no rebuild needed; see the `wiki` skill). **Before researching**: agents call `wiki_query` first. After discovering: agents write/update a page (`agentwire wiki new` scaffolds it). `raw/` is an optional verbatim-source inbox; archive a consumed source with `agentwire wiki done`.

## Handoffs

`/handoff` distills the current conversation into a shareable bundle: `ai-handoff.md` for another LLM and `show-the-story.html` for humans. The agent does the distillation in-context (free); the CLI/MCP renders deterministically. Outputs in `~/.agentwire/artifacts/handoff-<slug>/`. Full reference: [`docs/wiki/communication/handoff.md`](docs/wiki/communication/handoff.md).

## Usage-Limit Recovery

Deterministic (zero-LLM) recovery from the Claude Code usage-limit dialog: a launchd watchdog (`agentwire limits tick`, 60s) plus ensure's completion poll detect the dialog, park the session (option 1: stop and wait), parse the reset time, email the owner via Resend, and nudge the session to continue after reset. Park state under `~/.agentwire/usage-limit/` guards every surface — ensure exits 7 (`usage_limit`), the scheduler skips dispatch, the idle hook never reaps a parked session. CLI under `agentwire limits ...`. Full reference: [`docs/wiki/usage-limit-recovery.md`](docs/wiki/usage-limit-recovery.md).

## Prompt Routing

Interactive prompts (permission, plan-approval, AskUserQuestion) hitting a child session are routed as text to its parent/orchestrator — hook path for permissions (seconds), pane-sweep riding the limits watchdog for the rest (≤60s). Parent resolution: worker pane → pane 0; else creator recorded at `agentwire new`; else `.agentwire.yml` `parent:`. Answer ONLY with `agentwire prompts answer -s <session> --expect <hash> <key>` (compare-and-send; raw send-keys races the portal). Deliveries are safety-gated (never paste into a live menu, a bare shell, or a parked session). CLI under `agentwire prompts ...`. Full reference: [`docs/wiki/sessions/prompt-routing.md`](docs/wiki/sessions/prompt-routing.md).

## Fan-out Cohorts

**If you spawn child sessions, call `agentwire wait --children` (MCP `wait_children`) — never just "wait".** Idle ≠ done for a parent with outstanding children: the idle handler reads idle as done, prompts for a roll-up you can't write yet, and `/exit`s you while the children are still working (#852). Blocking inside the tool call isn't idle, so the hook never fires. Bounded (`--timeout`) and re-callable; exit 1 means children are still pending, so loop.

Every `agentwire new` / `worktree` **auto-enrolls** in the caller's cohort ledger (`~/.agentwire/cohorts/<parent>.json`) — no bookkeeping to forget. Cohort is **not** rooting: `created_by` drops the parent link for a cross-project spawn (#715), but lifecycle membership survives it, or a fan-out would be silently half-protected. Opt out with `--no-cohort`; an explicit `--kind orchestrator` never enrolls (durable by definition). `wait` collects each report from the inbox and *then* kills the child — the reverse order dead-letters the report and emails the owner via `gc_sender`. Two backstops: the idle-handler guard (fails open on a missing/corrupt/expired ledger) and the watchdog sweeper (reaps a cohort whose parent died, marks children that exited). Teardown is session-only and **skips worktree children entirely** — they hold a branch/open PR (teardown follows merge verification, #756) and are where review fix-ups get sent; `wait` reports them under `left_alive`, and `worktree --dangling` still flags an abandoned one. Full reference: [`docs/wiki/sessions/fan-out-cohorts.md`](docs/wiki/sessions/fan-out-cohorts.md).

## Polite Messaging

`agentwire msg` is the non-interrupting sibling of `agentwire send`. `send`/`session_send` paste into the prompt and press Enter immediately — and **clobber a human's half-typed draft** if the box is occupied. `msg` drops a typed JSON message into a per-recipient file inbox (`~/.agentwire/inbox/<session>/`) and the watchdog injects it **only when the input box is empty** (`prompt_router.prompt_is_empty`) and `safe_deliver` guards pass (not parked, agent pane, no live dialog). Drain rides the limits watchdog (≤60s), coalescing queued messages into one paste; deferred messages bump `attempts` and dead-letter after 40 — except *busy* reasons (`target_busy`, or the `queued_placeholder` "Press up to edit queued messages" state) defer **without** penalty so a legitimately-busy session never burns a report-back to death, while a recipient that *positively doesn't exist* dead-letters fast (`target_gone`, ~5 min — #694; `msg send` warns at enqueue time when the named target is gone). Delivery is **idempotent** (#621): a `send_verified` box-cleared confirm can false-negative even though the paste *landed*, so the drain dedups against the recipient's scrollback by full rendered line per-message (`session_ready.message_on_scrollback`) and `send_verified`'s Phase-2 confirm keys on *"the box no longer holds our text"* (not a spinner/echo) — a landed paste can't redeliver forever, and the same fix covers `notify-parent`/`session_send`. Escape hatches: `agentwire msg purge <session>` (MCP `msg_purge`) drops a wedged pending queue, `msg flush --force` force-drains past the empty-box gate, and `agentwire kill` GCs an exited sender's still-pending outbound (load-bearing kinds dead-letter+escalate). Typed kinds (`note`/`done`/`request`/`escalation`), `@all` broadcast (live agent sessions minus sender). Dead-lettered messages carry their drop reason + timestamp; load-bearing kinds (`done`/`request`/`escalation`) also **email the owner on dead-letter** (shared Resend wiring, best-effort) so the loss is never silent even unwatched. `agentwire msg dead [-s <session>]` (MCP `msg_dead`) lists them (no `-s` = GLOBAL, even from inside a session — #693), and `agentwire msg dead --purge [-s <session>] [--older-than 7d]` clears the graveyard (`doctor` surfaces it). CLI `agentwire msg send|inbox|dead|flush|purge`, MCP `msg_send`/`msg_inbox`/`msg_dead`/`msg_flush`/`msg_purge`. **Use `msg` for routine peer updates; reserve `send`/`session_send` for forcibly driving a session now.** Full reference: [`docs/wiki/sessions/messaging.md`](docs/wiki/sessions/messaging.md).

## Council

Multi-soul orchestrator sitting, **namespaced by `<name>`** so independent councils run concurrently: `agentwire-council-<name>` fans prompts out to lens sessions (`council-<name>-brain`, `council-<name>-conscience`, …), each replying take/ack/pass through a file inbox under `~/.agentwire/council/<name>/prompts/`; the orchestrator collects and synthesizes with attribution. Targeting: `--name` → cwd-repo-slug if it matches a live sitting → sole live sitting → else error+list; every command echoes which sitting it hit. The standard `soul` role self-excludes from any `council-*` session. `council minutes` renders a sitting's persisted record (question + attributed verbatim takes + optional `--synthesis`) into a self-contained HTML artifact at `~/.agentwire/artifacts/council-<name>-minutes/`; `council stop` renders it by default when any prompt exists (`--no-minutes` to skip). CLI under `agentwire council ...` (incl. `council list`), 7 MCP `council_*` tools. Full reference: [`docs/wiki/council.md`](docs/wiki/council.md).

## Briefing Mode

Asymmetric-verbosity orchestration: a terse, human-facing **`anchor`** (persona role; replaces orchestrator) fans out exhaustively-verbose **`correspondent`** worktrees (`worktree_create(roles="correspondent", prompt=…)`; stacks on the worker-worktree rail). Correspondents file deep reports into the dropbox (`agentwire research ensure` / `research_dir()` → `~/.agentwire/research/<anchor>/`) and signal **passively** — `msg send --kind ingest --ref <path>` is never auto-delivered, so it never drives the anchor. The anchor stays quiet until the human cues it, then `msg pull`s the pointers, reads the files, and **briefs across two channels in one call**: `say(text="<spoken headline>", display="<richer toast card>")` — different content per channel; the toast (`notify_user`) renders a safe markdown subset. Teardown via `worktree_remove` / `worktree_prune`. The notify-* family is split by target: `notify_user` (human toast), `notify_parent` (your orchestrator), `notify_event` (portal lifecycle). Full reference: [`docs/wiki/briefing-mode.md`](docs/wiki/briefing-mode.md).

## Large Parallel Refactors

Splitting one huge file across **parallel worktree sessions** (a worktree per disjoint slice, each PR'ing into a shared feature branch the orchestrator controls) has one load-bearing gotcha: the branches conflict on **positional interleaving**, not logic. Even when slices are logically disjoint, their functions interleave by *line position* in the shared file, so once one branch merges, the rest get large adjacency conflicts — and regenerating them all onto the same base does NOT fix it. The resolution is **regenerate-against-fresh-base, merged sequentially**: merge one, then each next branch takes the file fresh from the new tip, re-applies only its own removal (now contiguous), `git reset --soft <base>` to one clean commit, push. Plus: **foundation-first** (extract shared helpers to `core.py` + a registrar dispatch seam *before* fanning out), **verify with the FULL suite** (unit + integration — integration tests patch moved symbols), and **grep the real coupling graph** rather than trusting a recon model. Reach for this when the work is a big file-split across parallel agents; skip it for inline refactors or work with no shared file. Full reference: [`docs/wiki/internals/parallel-refactor.md`](docs/wiki/internals/parallel-refactor.md).

## Reference Skills

Reference detail lives in skills under `.claude/skills/` — invoke as needed:

| Skill | When to use |
|-------|-------------|
| `agentwire-cli` | Running or composing any `agentwire ...` shell command |
| `agentwire-mcp-tools` | Picking the right MCP tool from inside an agent session |
| `agentwire-config` | Editing `~/.agentwire/config.yaml` (TTS, channels, services, etc.) |
| `agentwire-project-config` | Editing `.agentwire.yml` (session config) / `.agentwire.tasks.yml` (tasks), roles, idle notifications |
| `agentwire-scheduler` | Scheduled task gates/schedule/priority |
| `agentwire-desktop-ui` | Editing portal static files (sidebar, windows, artifacts) |

## Docs

- CLI: `agentwire --help` or `agentwire <cmd> --help`
- **[`docs/wiki/INDEX.md`](docs/wiki/INDEX.md)** — feature reference manual (sessions, communication, scheduling, integrations, deployment, TTS, internals)

## Issue tracking

How work is tracked is a contributor preference, not something agentwire ships — the product imposes no PM mandate. For *this* repo, the convention is GitHub issues as the source of truth, with `Closes #N` in the PR body to link and auto-close. If you keep cross-repo tracking preferences in `~/.claude/rules/project-tracking.md`, those govern your own workflow and override this. Post-ship reference content (concepts, architecture, troubleshooting) belongs in `docs/wiki/`.
