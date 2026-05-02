# Glossary

> Living document. Update entries as concepts change. One line of definition + one of context, plus a link to the wiki page that goes deep.

Terms that show up across the docs without a single source-of-truth definition. Alphabetical.

## A — Artifact

An agent-generated HTML file written to `~/.agentwire/artifacts/` and served by the portal at `/artifacts/<filename>`. Used to share rich output (charts, dashboards, reports) across sessions and devices. → [Portal](internals/portal.md#artifacts).

## B — Bridge

A subtype of *channel* that runs as its own tmux service session and routes inbound messages from a platform (Discord, Slack, Telegram) to agentwire sessions. Bidirectional, long-lived, composable per channel/user. → [Channels](communication/channels.md).

## C — Channel

An integration that connects an external platform (email, SMS, Discord, Slack, Telegram, webhook, Quo) to agentwire sessions. Send-only channels are stateless; service channels (bridges) are long-lived. → [Channels](communication/channels.md).

## D — Damage Control

Security firewall: PreToolUse hooks block dangerous bash/edit/write operations using pattern rules in `agentwire/hooks/damage-control/rules/*.yaml` plus an optional user override at `~/.agentwire/damage-control/`. → [Damage control](internals/damage-control.md).

## E — Ensure Task

A headless agent task defined under `tasks:` in a project's `.agentwire.yml` and executed by `agentwire ensure`. Runs a full Claude Code session through `pre` → `prompt` → `on_task_end` → `post` phases with optional branch management. → [Scheduled workloads](scheduling/scheduled-workloads.md).

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

## O — Overnight Queue

A separate queue from the scheduler: human-prepared sessions captured via `agentwire overnight prepare` are dispatched within a configurable window with forked Claude conversation context. Best for judgment-heavy work that can't be expressed as recurring YAML. → [Scheduled workloads — Overnight Session Queue](scheduling/scheduled-workloads.md#overnight-session-queue).

## P — Pane

A tmux pane within a session. Convention: pane 0 is the *orchestrator*, panes 1+ are *workers*. Workers auto-kill after sending their final idle notification.

## P — Pi

The third-party `@mariozechner/pi-coding-agent` CLI. Powers `pi-<provider>` session types and `runner: pi` workflow nodes. Faster and cheaper than Claude Code for non-Anthropic models, but no MCP and no hook integration. → [Pi sessions](sessions/pi.md).

## P — Portal

The agentwire web UI + REST/WebSocket API at `https://localhost:8765`. Wraps CLI commands rather than reimplementing them — every endpoint shells out to `agentwire <cmd> --json`. → [Portal](internals/portal.md).

## P — Project Config

`.agentwire.yml` at a project root. Defines `type:` (session type), `roles:`, `voice:`, `parent:`, and named `tasks:`. Picked up automatically when `agentwire new` targets a path that contains it. → `agentwire-project-config` skill in `.claude/skills/`.

## R — Role

A reusable system-prompt persona stored at `~/.agentwire/roles/<name>.md`. Listed in `roles:` (project config) or `--role` (CLI). Roles are appended to the agent's system prompt at session creation. → `agentwire-config` skill.

## S — Scheduled Task

An entry in `~/.agentwire/scheduler.yaml` that fires on a schedule (`every:`, `at:`, `after:`). Sets exactly one of `task:` (delegates to `agentwire ensure`) or `workflow:` (delegates to the workflow engine in-process). → [Scheduled workloads](scheduling/scheduled-workloads.md).

## S — SDK Session

Sessions of type `sdk-bypass` / `sdk-prompted` / `sdk-restricted`. They run inside the `agentwire/sdk/` package (a thin wrapper around `claude-agent-sdk`) and surface as the `agentwire repl` Textual TUI. → [REPL TUI walkthrough](sessions/repl-tui.md).

## S — Session

A tmux session running an AI agent (claude-*, claudeglm-*, pi-*, sdk-*, bare). Created with `agentwire new`. Identified by name, with `@machine` suffix for remote sessions. → [Sessions index](INDEX.md#sessions).

## T — Tunnel

An SSH or Cloudflare Tunnel that exposes a local agentwire service (TTS server, portal) on a remote machine or to the public internet. → [Remote access](deployment/remote-access.md).

## V — Voice

A TTS reference WAV (10–30 s) stored in `~/.agentwire/voices/`. Selected per-session via `voice:` in project config or per-call via `--voice`. The `default` voice is used when nothing is specified. → [Self-hosted TTS](tts/tts-self-hosted.md#voices).

## W — Worker

An agent in a pane 1+ of a session, spawned by the orchestrator (typically via the MCP `pane_spawn` tool) for a bounded task. Auto-kills after sending its idle notification. Pane numbering reflects spawn order.

## W — Workflow

A YAML file under `agentwire/workflows/examples/` (bundled) or `~/.agentwire/workflows/defs/` (user) describing a DAG of nodes. Each node runs against the `pi` runner or the `anthropic` runner; nodes flow templated outputs to dependents. Run with `agentwire workflow run <name>`. → [Pi workflows](scheduling/workflows.md).
