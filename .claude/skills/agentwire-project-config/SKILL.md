---
name: agentwire-project-config
description: Reference for per-project `.agentwire.yml` (declarative posture/roles/voice/parent/worktree, agent-writable) and the separate protected `.agentwire.tasks.yml` (pre/prompt/post/on_task_end/shell/branch-management task schema, authored via propose-and-promote); hierarchical idle notifications with worker summary files and queue processor; role system. Use when configuring a project for agentwire, wiring up scheduled tasks, defining worker/orchestrator relationships, or debugging task execution.
---

# `.agentwire.yml` Project Config

Each project can have a `.agentwire.yml` in its root directory. This configures posture, roles, voice, and parent for that project.

**Split from task-execution config (#720).** `.agentwire.yml` is now PURELY
declarative — `posture`/`roles`/`voice`/`parent`/`worktree`, no execution vector —
so it's agent-writable. Named `tasks:` (the code the scheduler actually runs
via `shell=True`: `pre`/`post`/`on_task_end`/`shell`) live in a separate file,
**`.agentwire.tasks.yml`**, which is protected control-plane. See
[Task Schema](#task-schema-agentwiretasksyml) below for how to author it.

## Gitignore it (don't commit it)

**`.agentwire.yml` should be gitignored, not committed.** Two reasons:

1. **A tracked copy breaks worktree dispatch subtly.** Worktree-dispatched runs (scheduler tasks, worktree sessions) check out HEAD — if the file is tracked, uncommitted live edits are invisible to the run, which silently executes the stale committed version. The failure mode is confusing: the live file looks right, the run behaves wrong.
2. **It's personal/live config, not project code.** Voice choices, email recipients, schedules, machine-specific roles — none of that belongs in a repo, especially a public one.

Worktree runs still get the file: `projects.worktrees.copy_files` (default `[".env", ".agentwire.yml", ".agentwire.tasks.yml"]`) copies gitignored configs from the main checkout into every new worktree as live files — the live file always wins, no drift.

AgentWire enforces this automatically: whenever it writes `.agentwire.yml` into a git repo (`agentwire projects create`, `agentwire new --persist`, portal project settings), it appends the file to the project's `.gitignore` unless it's already ignored — or already *tracked*. A tracked file is left alone: committing is a valid choice for teams that want shared, versioned task definitions, but they're opting into the HEAD-checkout behavior above — worktree runs use the committed version, never live edits. To switch an existing project to the recommended setup: `git rm --cached .agentwire.yml`, add it to `.gitignore`, commit. `agentwire tasks promote` gitignores `.agentwire.tasks.yml` (via a `.agentwire.tasks*.yml` glob, covering the staging draft too) the same way, on first promote.

**Format is FLAT (no nesting):**

```yaml
# Session with voice and agentwire awareness
posture: bypass
roles:
  - agentwire
  - voice
voice: may
parent: main  # Notify parent session when idle (optional)
```

```yaml
# WRONG - don't nest under "session:"
session:
  posture: bypass
  roles: [...]  # This won't be loaded!
```

| Field | Values | Description |
|-------|--------|-------------|
| `posture` | `bypass`, `prompted`, `restricted`, `readonly`, `auto` (or `bare`) | Permission mode the agent runs under. Use `auto` for unattended work — classifier blocks dangerous actions. |
| `roles` | List of role names | Roles to load (from bundled or `~/.agentwire/roles/`) |
| `voice` | Voice name | TTS voice for this project |
| `parent` | Session name | Parent session for hierarchical notifications |
| `worktree` | `dir` / `base` mapping | Per-project overrides for `agentwire worktree` (see below) |

`shell`/`tasks` used to live here — they moved to the separate, protected
`.agentwire.tasks.yml` (#720). See [Task Schema](#task-schema-agentwiretasksyml).

## Worktree overrides (#705)

A project can override where `agentwire worktree` puts its worktrees and which
branch they fork from, without touching the global config — the
monorepo/develop-base shop is the canonical case:

```yaml
worktree:
  dir: ~/work-trees     # overrides global worktree.worktree_dir for THIS project
  base: develop         # overrides global worktree.default_base for THIS project
```

Precedence (most specific wins): per-invocation CLI/MCP `--base` flag →
project `.agentwire.yml` `worktree:` block → global `~/.agentwire/config.yaml`
`worktree:` → built-ins (`~/worktrees`, repo origin/HEAD). The nesting shape is
unchanged — `dir` only moves the root: `<dir>/<project>/<name>/`. ALL worktree
subcommands (create/list/status/remove/prune) resolve through the same
project-scoped dir, and registry entries record the resolved path, so existing
worktrees survive an override change. Unknown keys in the block warn to stderr
but don't fail.

> **No safety config in `.agentwire.yml` (#466/#467).** `.agentwire.yml` is
> agent-writable, so it carries **zero** damage-control policy. The kill switch,
> `disabled_rules`, `unattended_allow` defaults, **and the per-project `allowed_paths`
> allowlist** all live in the protected, agent-unwritable `.damagecontrol.yml` at
> the repo root (and global `~/.agentwire/damagecontrol.yml`) — edited host-side
> only. The allowlist is the one knob that overrides the protected-control-plane
> check, so it must itself sit behind that protection. See
> [`docs/wiki/internals/damage-control.md`](../../docs/wiki/internals/damage-control.md).

## Task Schema (`.agentwire.tasks.yml`)

Tasks are defined in the separate, **protected** `.agentwire.tasks.yml` for use
with `agentwire ensure` (#720). It holds every field that's actually code the
scheduler runs via `shell=True` — `shell`/`pre`/`post`/`on_task_end` —
so it sits at the same damage-control tier as `.damagecontrol.yml`: a policed
agent cannot write it directly with Edit/Write/Bash.

**Authoring it — propose-and-promote** (mirrors the worktree → PR → review →
merge model, because task defs ARE executable code):

1. An agent drafts to the **unprotected** staging file
   `.agentwire.tasks.proposed.yml` with its normal file tools.
2. A human runs `agentwire tasks review [session]` — prints a diff against the
   live file plus every shell-bearing field the draft would run, and any
   validation issues.
3. The human runs `agentwire tasks promote [session] [--yes]` — copies the
   vetted draft into the live `.agentwire.tasks.yml` (agentwire itself,
   host-trusted, does the write) and deletes the draft.

Both commands are **host-only by design** — and `promote` is **hard-gated**,
not just discouraged: it's not exposed as an MCP tool (an MCP tool that
shelled out to it would bypass the Bash-tool protection entirely — see
[Outbound MCP tool gating](../../docs/wiki/internals/damage-control.md#outbound-mcp-tool-gating-457)),
it's blocked as a Bash command even with `# allow:` or the kill switch off
(`PROTECTED_COMMAND_PATTERNS` in `safety/_core.py`), and the function itself
refuses to run under `AGENTWIRE_UNATTENDED=1` or without a genuine host signal
(a real interactive terminal, or the explicit `AGENTWIRE_ALLOW_TASKS_PROMOTE=1`
opt-in for your own non-interactive script) — `--yes` only skips the
confirmation prompt, it never substitutes for that. Run `promote` from your
own terminal; it cannot be made to run through the agent.

```yaml
# .agentwire.tasks.yml
shell: /bin/sh  # Project-level default shell

tasks:
  morning-briefing:
    shell: /bin/bash           # Task-level override
    priority: 10               # Pipeline ordering (lower = higher priority, default: 99)
    retries: 2                 # Retry on failure (default: 0)
    retry_delay: 30            # Seconds between retries (default: 30)
    idle_timeout: 30           # Seconds of idle before completion (default: 30)
    exit_on_complete: true     # Exit session after completion (default: true)
    role: task-runner          # Role override for this task (optional)
    pre:                       # Data gathering (NO {{ }} - these PRODUCE variables)
      weather: "curl -s wttr.in/?format=3"
      calendar:
        cmd: "gcal-cli today --json"
        required: true         # Fail if empty (default: false)
        validate: "jq . > /dev/null"  # Validation command
        timeout: 30            # Command timeout
    prompt: |                  # Main prompt (supports {{ variables }})
      Weather: {{ weather }}
      Calendar: {{ calendar }}
      Summarize my day.
    on_task_end: |             # Optional: after system summary
      Read {{ summary_file }}.
      If complete, save to ~/briefings/{{ date }}.md
    post:                      # Commands after completion
      - "echo 'Status: {{ status }}'"
    output:
      capture: 50              # Lines to capture
      save: ~/logs/{{ task }}.log

    # Branch management (for async agent workflows)
    starting_ref: main         # Git ref to checkout before task runs
    work_branch: agent/task    # Branch for agent's work (default: agent/<task>-<date>)
    pr_target: main            # PR target branch (default: starting_ref)
    pr_draft: true             # Create as draft PR (default: true)

    # Context inheritance
    starting_session: ctx-loaded  # Fork Claude context from this session before running

    # Unattended safety (scheduler, no human present)
    unattended_allow:             # damage-control rule ids this task may run unattended,
      - tooldef.terraform-apply-planned-changes-to-infrastructure  # extends the global default
```

**`unattended_allow`** (per-task): when the scheduler dispatches a task headless
(`AGENTWIRE_UNATTENDED=1`), the damage-control hook resolves `ask`-tier commands
by **failing closed** — block + email the owner — unless the matched rule id is
on the allowlist. The default allowlist lets a task work and open a PR
(`git.add`/`git.commit`/`git.push`/`gh.pr-create`); list extra rule ids here to
permit a normally-gated action (deploy, DB write, outbound email) for **this task
only**. Blocked actions name the exact rule id (in the email and `agentwire safety
logs`) so widening is copy-paste. Hard blocks (`rm -rf`, `git push --force`) and
interactive sessions are unaffected. See `docs/wiki/internals/damage-control.md`.

**Built-in variables:**
- `{{ date }}`, `{{ time }}`, `{{ datetime }}` - Current date/time
- `{{ session }}`, `{{ task }}`, `{{ project_root }}` - Task identity
- `{{ attempt }}` - Current attempt number (1-based)
- `{{ status }}`, `{{ summary }}`, `{{ summary_file }}` - After completion
- `{{ output }}` - Captured session output (in post phase)
- `{{ work_branch }}`, `{{ pr_url }}` - Branch/PR after branch management (in post phase)
- `{{ var_name }}` - Pre-command outputs

**Exit codes:** 0=complete, 1=failed, 2=incomplete, 3=lock conflict, 4=pre failure, 5=timeout, 6=session error

## Parent Resolution (prompt routing + notifications)

For prompt routing (#276), a session's parent resolves in this order: **creator recorded at `agentwire new` time** (session metadata, `--created-by` to override) → **`.agentwire.yml` `parent:`** → none. Worker panes always route to pane 0 of their own session. Interactive prompts (permission/plan/AskUserQuestion) hitting a child are texted to that parent; answer only via `agentwire prompts answer` (guarded), never raw send-keys.

## Hierarchical Idle Notifications

When a session goes idle, it notifies up the hierarchy via `agentwire notify-parent` (text-only, no audio):

```
parent session ← receives "[ALERT from child] ..."
    ↑ notify-parent --to parent
child session   ← receives "[ALERT from pane N] ..."
    ↑ auto-notify pane 0
worker panes
```

**Worker summary files:**
- Workers write summaries to `.agentwire/worker-{pane}.md` before going idle
- Summaries include: task, status, what worked, what didn't, notes for orchestrator
- Orchestrators read these files to understand worker results

**Auto-exit (workers auto-kill on idle):**
- Worker panes (index > 0) automatically exit when idle
- Use `parent: <session-name>` in `.agentwire.yml` for child → parent notifications

**Queue system files:**
- `~/.agentwire/queue-processor.sh` - Processes queue with 15s delays between alerts
- `~/.agentwire/queues/{session}.jsonl` - Per-session notification queues

**Worker idle sequence:**
1. `session.idle` fires → wait 2s (let agent settle)
2. Worker writes summary to `.agentwire/worker-{pane}.md`
3. Queue notification to `{session}.jsonl`
4. Start queue processor if not running
5. Worker auto-exits

**Idle notifications** are handled via `~/.claude/hooks/idle-handler.sh`.

**Creating a project with roles:**

```bash
# Option 1: Create .agentwire.yml first, then create session
echo "posture: bypass
roles:
  - agentwire
  - voice" > ~/projects/myproject/.agentwire.yml

agentwire new -s myproject -p ~/projects/myproject

# Option 2: Specify roles on command line, --persist saves to .agentwire.yml
agentwire new -s myproject -p ~/projects/myproject --roles agentwire,voice --persist
```

By default, `agentwire new` flags (`--posture`, `--roles`) are session-level overrides only and never save to `.agentwire.yml`. Use `--persist` to opt in to saving.

## Role System

Roles define agent behavior and are composable. Mix and match roles in `.agentwire.yml` to configure orchestrators, workers, or specialized agents.

**ROLE vs TOPOLOGY (#716):** the session `kind` (`orchestrator` | `worker`) is a pure authority/etiquette axis — it says nothing about WHERE the session runs. TOPOLOGY (main checkout / worktree branch / pane) is separate and independent. `worker`'s intrinsic etiquette FILE still varies by topology, though: a worktree-topology worker gets `worker-worktree` (isolation, draft-PR, notify, keeps voice); a pane/main-topology worker gets `worker` (headless, exit-summary, auto-kill). Neither is user-selectable directly — they're resolved from `kind` + topology by `resolve_roles`.

**Available roles:**

| Role | Purpose |
|------|---------|
| `agentwire` | Core session/pane/MCP tools awareness |
| `orchestrator` | Long-lived project orchestrator — plans, delegates, reviews results. Topology-invariant (same role file on main or a worktree). |
| `voice` | Voice communication (speak/listen) |
| `worker` | Receive tasks, execute autonomously, report back. Intrinsic etiquette for a pane (`agentwire spawn`) or a main-topology session — headless, exit-summary, auto-kill. Not directly selectable via `--roles` as a *replacement* for the worktree flavor; it's resolved from kind + topology. |
| `worker-worktree` | Intrinsic etiquette for `agentwire worktree` sessions (isolation, no rebuild/restart, verify in-worktree, draft PR + notify-back, keeps voice) — auto-injected when kind=worker on worktree topology, not configurable |
| `task-runner` | Scheduled task execution |
| `chatbot` | Conversational personality |
| `init` | Setup wizard behavior |

Use `agentwire roles list` to see available roles. Roles are bundled in `agentwire/roles/` and can be composed freely in `.agentwire.yml`.
