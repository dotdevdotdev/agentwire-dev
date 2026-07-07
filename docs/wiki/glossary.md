# Glossary

> Living document. Update entries as concepts change. One line of definition + one of context, plus a link to the wiki page that goes deep.

Terms that show up across the docs without a single source-of-truth definition. Alphabetical.

## A — Artifact

An agent-generated HTML file written to `~/.agentwire/artifacts/` and served by the portal at `/artifacts/<filename>`. Used to share rich output (charts, dashboards, reports) across sessions and devices. → [Portal](internals/portal.md#artifacts).

## C — Channel

An outbound notification integration — a session pushes a message to an external platform (email via Resend, SMS via Quo / OpenPhone). Stateless `SendOnlyChannel` subclasses; no inbound surface. Inbound user input flows through the portal, not channels. → [Channels](communication/channels.md).

## D — Damage Control

Security firewall: PreToolUse hooks block dangerous bash/edit/write operations using pattern rules in `agentwire/hooks/damage-control/rules/*.yaml` plus an optional user override at `~/.agentwire/damage-control/`. → [Damage control](internals/damage-control.md).

## E — Ensure Task

A headless agent task defined under `tasks:` in a project's protected `.agentwire.tasks.yml` and executed by `agentwire ensure`. Runs a full Claude Code session through `pre` → `prompt` → `on_task_end` → `post` phases with optional branch management. → [Scheduled workloads](scheduling/scheduled-workloads.md).

## F — Fork (Session Fork)

`agentwire fork` (or the `session_fork` MCP tool) creates a new session whose Claude Code conversation history is copied from an existing session via `--resume <id> --fork-session`. Used to spawn parallel worktree sessions with shared context. → [Portal](internals/portal.md#session-actions).

## G — Gate

A precondition on a scheduled task that must evaluate true before the task fires. Three types: `command:` (run shell, check output/exit), `git_diff:` (paths changed since last run), `git_commit:` (HEAD advanced on tracked paths). All gates AND together. → [Scheduled workloads](scheduling/scheduled-workloads.md).

## I — Idle Notification

Fired by `~/.claude/hooks/idle-handler.sh` when an agent goes idle. Workers (panes 1+) notify pane 0 and auto-kill. Orchestrators (pane 0) notify the session named in `parent:`. → top-level [CLAUDE.md](../../CLAUDE.md).

## L — Lock

A per-session mutex (`~/.agentwire/locks/<session>.lock`) acquired by `agentwire ensure` to prevent concurrent task runs against the same session. Cleared on completion or via `agentwire lock clean`. → [Scheduled workloads](scheduling/scheduled-workloads.md).

## M — Machine

A registered remote host in `~/.agentwire/machines.json` (`id`, `host`, `user`, `projects_dir`). Sessions on a machine are addressed as `<session>@<machine>`. → [Remote machines](deployment/remote-machines.md).

## O — Orchestrator

The agent in pane 0 of a session. Coordinates work, spawns workers in panes 1+ (`pane_spawn`), receives idle notifications from workers, and routes alerts via `parent:` to the user-facing session.

## P — Pane

A tmux pane within a session. Convention: pane 0 is the *orchestrator*, panes 1+ are *workers*. Workers auto-kill after sending their final idle notification.

## P — Portal

The agentwire web UI + REST/WebSocket API at `https://localhost:8765`. Wraps CLI commands rather than reimplementing them — every endpoint shells out to `agentwire <cmd> --json`. → [Portal](internals/portal.md).

## P — Project Config

`.agentwire.yml` at a project root. Purely declarative — `type:` (session type), `roles:`, `voice:`, `parent:`, `worktree:` — with zero execution vector, so it's agent-writable (#720). Picked up automatically when `agentwire new` targets a path that contains it. **Keep it gitignored** — it's personal config, and a tracked copy makes worktree-dispatched runs use the stale committed version instead of live edits (`projects.worktrees.copy_files` seeds the live file into worktrees). Named `tasks:` live in the separate, protected `.agentwire.tasks.yml` instead — see [Ensure Task](#e--ensure-task) and [Damage control](internals/damage-control.md#task-execution-config-split-agentwiretasksyml-720). → `agentwire-project-config` skill in `.claude/skills/`.

## R — Role

A reusable system-prompt persona stored at `~/.agentwire/roles/<name>.md`. Listed in `roles:` (project config) or `--role` (CLI). Roles are appended to the agent's system prompt at session creation. → `agentwire-config` skill.

## S — Scheduled Task

An entry in `~/.agentwire/scheduler.yaml` that fires on a schedule (`every:`, `at:`, `after:`). Delegates to `agentwire ensure` via `task:` + `session:` + `project:`. → [Scheduled workloads](scheduling/scheduled-workloads.md).

## S — Session

A tmux session running an AI agent (claude-*, bare). Created with `agentwire new`. Identified by name, with `@machine` suffix for remote sessions. → [Sessions index](INDEX.md#sessions).

## S — Soul

The bundled default-personality role (`agentwire/roles/soul.md`) — voice, restraint, ask-vs-proceed defaults. Always injected last into every human-facing session's role list, on top of whatever roles resolve. Headless roles (worker, task-runner, notifications) and `soul`/`soul-*` sessions are excluded. Opt out globally with `session.inject_soul: false` or per session with `--no-soul`; shadow the content per project via `.agentwire/roles/soul.md`. → `agentwire-config` skill.

## T — Tunnel

An SSH or Cloudflare Tunnel that exposes a local agentwire service (TTS server, portal) on a remote machine or to the public internet. → [Remote access](deployment/remote-access.md).

## V — Voice

A TTS reference WAV (10–30 s) stored in `~/.agentwire/voices/`. Selected per-session via `voice:` in project config or per-call via `--voice`. The `default` voice is used when nothing is specified. → [Self-hosted TTS](voice/tts-self-hosted.md#voices).

## W — Worker

An agent in a pane 1+ of a session, spawned by the orchestrator (typically via the MCP `pane_spawn` tool) for a bounded task. Auto-kills after sending its idle notification. Pane numbering reflects spawn order.

