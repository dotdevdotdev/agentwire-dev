# AgentWire

Voice interface for AI coding agents. Push-to-talk from any device to tmux sessions running Claude Code.

**No Backwards Compatibility** - Pre-launch, no customers. Change things completely, no legacy fallbacks.

**Always-loaded rules only; design narratives live in `docs/wiki/`.** Before touching a subsystem, read its linked page — the "why" and incident history are there.

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

Rebuild alone = stale static files. Restart alone = stale Python. The MCP server runs as a separate process started by Claude Code — session restart required after rebuild to pick up MCP changes. `rebuild` now refuses (warn + `--force` to override) when the checkout is behind `origin/main`, and `agentwire doctor` flags a behind-main checkout, and any of `~/.agentwire/` · `.env` · `portal.token` · `machines.json` readable beyond its owner (`doctor --yes` tightens them; #887).

## Tests

The PR gate has to stay fast or it gets bypassed. Rules for adding tests:

- **One assertion repeated over N inputs is ONE test.** Loop, collect the offenders, `assert not bad, bad` — a failure then lists every bad input at once. Never `parametrize` a range or a cross-product just to get N test items.
- **A test must fail against the bug it guards.** Mutate the code it protects once and watch it go red; a pin that passes against the behavior it was written to forbid proves nothing.
- **Test behavior, not text.** No tests that grep source files or docs prose for strings — they fail on refactors and pass on regressions.
- **No real waits, no real tmux in the fast set.** Stub the probe or the clock. A test that genuinely needs a real tmux or real git process is marked `requires_tmux` / `slow`: PRs run `-m "not requires_tmux and not slow"` with no coverage; pushes to main run everything hermetic with coverage.

## CLI is the Single Source of Truth

All session/machine logic lives in CLI commands (`agentwire/__main__.py`). The portal (`agentwire/server.py`) is a thin wrapper that:

1. Calls CLI via `run_agentwire_cmd(["command", "args"])`
2. Parses JSON output (`--json` flag)
3. Adds WebSocket/real-time features

**When adding new functionality:**
1. Implement in CLI first with `--json` output
2. Portal calls CLI, doesn't duplicate logic
3. Never bypass CLI with direct tmux/subprocess calls

**CLI layout (#495):** shared helpers (machine config, SSH `_run_remote`, JSON output, session resolution) live in `agentwire/core.py`; each command group is its own `agentwire/<domain>_cli.py` exposing `register_<domain>_parser(subparsers)`. Adding a command = a new `*_cli.py` + appending its registrar to `_REGISTRARS`, not editing a god-file. Portal git endpoints shell out to `agentwire repo-info` / `agentwire branches` (`repo_cli.py`) rather than embedding git/SSH logic.

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
| **Worktree session** | `agentwire worktree <name> -p <repo>` | **Standalone tmux session**, new branch `<name>` from origin/main. Survives independently; report-back via `agentwire notify-parent --to <orchestrator>`. Role defaults to `worker` (`--kind orchestrator` makes a durable project window). The `worker-worktree` etiquette role is auto-injected, so first prompts only need the task. |
| **Worker pane** | `agentwire spawn --branch <name>` | A **pane inside the current session** (pane 1+), worktree on `<name>`. Inherits the session's dashboard; idle hook reaps it. |

When the owner says "worktree session", they mean the standalone session (`agentwire worktree`), **never** `spawn` panes. Panes are for small watched subtasks; worktree sessions for parallel autonomous work. Every spawn defaults to **bypass** posture (agentwire is not a guard), so no `--posture` is needed.

**`agentwire orchestrator [name] -p <project>`** is sugar for `worktree --kind orchestrator` — worktree topology + orchestrator role + rooted by default. `name` defaults to `"orchestrator"`.

**Rooting and delegation (#715, #716, #1259, #1261):** `worktree`/`new` parent the new session to the caller only for same-project spawns; a cross-project spawn without a task prompt is a standalone root; a spawn that carries a task prompt (`--prompt` / `--first-message` / MCP `prompt`) parents to the CALLER in any project, because a child you are waiting on must report to you; an explicit `--kind orchestrator` roots. `--created-by <name>` / `--created-by ''` override. An explicit `--created-by` that isn't a live session fails at spawn. **Your session name is NOT your repo name** — `agentwire list` / `tmux display -p '#S'` tells you; `msg send --to parent` is rejected. Detail: [`docs/wiki/sessions/worktree-sessions.md`](docs/wiki/sessions/worktree-sessions.md), [`docs/wiki/sessions/prompt-routing.md`](docs/wiki/sessions/prompt-routing.md).

**Dangling PRs (#716):** `agentwire worktree --dangling` (and `doctor`) flag LIVE worker sessions with an OPEN PR and no live recorded parent — a worker correctly refuses to self-merge, so its PR dangles.

**Never hand-build a worktree path or session name (#837, #855, #868, #878).** Ask the helpers: `worktree.find_git_worktree` (real path, from git), `worktree.create_and_register_worktree` (the one creator), `worktree.worktree_session_name()` / `tmux_safe_name()` (tmux rewrites `.` and `:` to `_`), `worktree.teardown_session_note()` (states a session's fate). Never write `~/worktrees/<project>/<name>` or `f"{project}-{name}"` in new code. Detail: [`docs/wiki/sessions/worktree-sessions.md`](docs/wiki/sessions/worktree-sessions.md).

**Conversation identity is RECORDED, not reconstructed (#871).** agentwire mints the UUID and records it in `~/.agentwire/sessions/<name>/metadata.json`. One writer (`core.record_session_launch()`, called once per tmux launch path; `spawn` does not), one reader (`core.load_session_metadata()`), one resumability probe (`history.locate_conversation`). Never re-pass a recorded `--session-id` (hard-errors; test launch changes with a SECOND launch, #901). **`agentwire restart -s <session>`** relaunches the same conversation with `--resume`, nothing on disk touched (`recreate` deletes the worktree dir). Detail: [`docs/wiki/sessions/conversation-identity.md`](docs/wiki/sessions/conversation-identity.md).

**Teardown is two questions, not one (#941).** (1) **Is the work durable?** — committed and pushed. That authorizes tearing down the *session and worktree*; `--remove` refuses a DIRTY worktree (`--discard-changes` overrides) and a worktree with a live unresolved session inside (#954). (2) **Has it been integrated?** — merged, issue CLOSED. That authorizes deleting the *branch*; verify merge BEFORE branch deletion, never teardown-then-check. `--remove` refuses an unmerged branch; `--force-delete-branch` overrides that but not an OPEN PR on the branch (#756; `--close-pr-branch` is the explicit escape). Same guard on `--prune --gc-merged`. Recovery recipe for a branch whose PR got closed: [`docs/wiki/sessions/worktree-sessions.md`](docs/wiki/sessions/worktree-sessions.md).

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
| `logs/` | Logs |

Per-project config lives in `.agentwire.yml` at the project root — **keep it gitignored** (personal config; a tracked copy makes worktree-dispatched runs silently use the stale committed version). `.agentwire.yml` is purely declarative (type/roles/voice/parent/worktree) and agent-writable; named tasks (pre/post/on_task_end/shell) live in the separate, protected sibling `.agentwire.tasks.yml`, authored via `agentwire tasks review`/`promote` (#720). See `agentwire-project-config` skill for fields and task schema.

## Key Patterns

- **agentwire sessions** coordinate via voice, delegate to workers
- **worker panes** spawn within the orchestrator's session (visible dashboard)
- **Pane 0** = orchestrator, **panes 1+** = workers
- **AgentWire is not a guard.** Sessions default to the `bypass` posture; no claim anywhere that a hook makes that safe. Screening, if installed, is a separate project's job.
- **Unattended marker** — scheduler dispatches are marked `AGENTWIRE_UNATTENDED=1` (children inherit it) and `unattended_allow` is passed through as an opaque string (`agentwire/unattended.py`, [`unattended.md`](docs/wiki/internals/unattended.md)). It is a public signal for any guard; agentwire reads no grammar from it.
- **No guard code lives here (#1313).** agentwire has no `safety` CLI, MCP tools, rules or engine dependency in its code; screening is the separate `damage-control-dev` project (its `agentwire` pack covers agentwire-specific protections). `hooks install` registers no engine and never removes an existing registration; the permission hook never auto-approves in any mode. Migrating an existing install: [`migrating-to-standalone-damage-control.md`](docs/wiki/internals/migrating-to-standalone-damage-control.md).
- **Merged is not deployed (#936 + #916).** Only the canonically installed tool may write machine-global files (`agentwire/provenance.py`; override `--allow-foreign-source`, deliberately no env override): install and `rebuild` REFUSE from a worktree or foreign package, because that venv dies with the worktree. Machine-global links go through `hooks_cli._link_into_claude`; tests never write under the real `~/.claude` / `~/.agentwire`. A deployment claim must name the path that EXECUTES.
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

## Subsystem rules (detail in the wiki)

- **Usage-limit recovery** — `agentwire limits ...`; a parked session (ensure exit 7 `usage_limit`) is never dispatched to or reaped. [`usage-limit-recovery.md`](docs/wiki/usage-limit-recovery.md).
- **Expired login** — detected from the TRANSCRIPT's structured `error` field on the LAST assistant turn, never from pane text (ensure exit 8 `auth_expired`, never retried). [`auth-expired.md`](docs/wiki/auth-expired.md).
- **Idle is not done while a background task runs** — `idle-handler.sh` asks `agentwire idle-gate` first and defers; unknown means HOLD, never reap (#1103). [`troubleshooting.md`](docs/wiki/internals/troubleshooting.md#idle-notice-missing-while-a-background-task-runs-1103).
- **Prompt routing** — interactive prompts (permission, plan-approval, AskUserQuestion, resume-from-summary) hitting a child route as text to its parent. Answer ONLY with `agentwire prompts answer -s <session> --expect <hash> <key>` (raw send-keys races the portal). [`prompt-routing.md`](docs/wiki/sessions/prompt-routing.md).
- **Fan-out cohorts** — **if you spawn child sessions, call `agentwire wait --children` (MCP `wait_children`), never just "wait"**: idle ≠ done for a parent with outstanding children (#852). Spawns auto-enroll in your cohort. `agentwire fleet [--json]` is the read-only view. [`fan-out-cohorts.md`](docs/wiki/sessions/fan-out-cohorts.md), [`fleet.md`](docs/wiki/sessions/fleet.md).
- **Polite messaging** — use `agentwire msg` (MCP `msg_send`, …) for routine peer updates; it delivers only when the recipient's input box is empty. `send`/`session_send` paste + Enter immediately and clobber a half-typed draft — reserve them for forcibly driving a session. Report-back: `msg send --kind done`. [`messaging.md`](docs/wiki/sessions/messaging.md).
- **Council** — `agentwire council ...` (7 MCP `council_*` tools), namespaced by `<name>`; takes are attributed verbatim. [`council.md`](docs/wiki/council.md).
- **Briefing mode** — terse `anchor` + verbose `correspondent` worktrees filing to `research_dir()`, signalling passively with `msg send --kind ingest`. [`briefing-mode.md`](docs/wiki/briefing-mode.md).
- **Large parallel refactors** — parallel worktrees on one shared file conflict on positional interleaving; foundation-first, merge sequentially, regenerate each branch against the fresh base, verify with the FULL suite. [`parallel-refactor.md`](docs/wiki/internals/parallel-refactor.md).

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
